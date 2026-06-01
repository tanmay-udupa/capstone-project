import sys
import os

import requests
import base64
import datetime
import time
import json
import pymssql

from collections import defaultdict
from azure.storage.blob import BlobServiceClient

# ---------------- CONFIG ----------------
ORG = "AVEVA-VSTS"
PROJECT = "Mobile"
PAT = os.environ["ADO_PAT"]

RUNS_CONTAINER = "pipeline-runs-raw"

SQL_SERVER = "capstone-sqlserver.database.windows.net"
SQL_DB = "pipelinedb-dev"
SQL_USER = "sqladmin"
SQL_PASSWORD = os.environ["SQL_PASSWORD"]

BLOB_CONNECTION_STRING = os.environ["BLOB_CONNECTION_STRING"]
# ----------------------------------------

if len(sys.argv) > 1:
    PROJECT = sys.argv[1]
if len(sys.argv) > 2:
    MAX_RUNS = int(sys.argv[2])
else:
    MAX_RUNS = 1000


# ================================================================
# AUTH & CONNECTION
# ================================================================

blob_service = BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)

def auth_header():
    token = base64.b64encode(f":{PAT}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def sql_conn():
    return pymssql.connect(
        server=SQL_SERVER,
        user=SQL_USER,
        password=SQL_PASSWORD,
        database=SQL_DB,
        tds_version="7.4",
    )

def get_or_reconnect(conn):
    try:
        c = conn.cursor()
        c.execute("SELECT 1")
        c.close()
        return conn
    except Exception:
        print("  [SQL] Reconnecting...")
        return sql_conn()


# ================================================================
# BLOB HELPERS
# ================================================================

def save_run_json(run_id, data):
    blob = blob_service.get_blob_client(
        container=RUNS_CONTAINER,
        blob=f"{PROJECT}/{run_id}.json",
    )
    blob.upload_blob(json.dumps(data), overwrite=True)


# ================================================================
# ADO API HELPERS
# ================================================================

def ado_get(url, headers=None):
    headers = headers or auth_header()
    for attempt in range(3):
        r = requests.get(url, headers=headers)
        if r.status_code == 429:
            wait = 10 * (attempt + 1)
            print(f"  [429] Rate limited. Waiting {wait}s before retry {attempt + 1}/3...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r
    raise Exception(f"ADO request failed after 3 retries: {url}")

def get_builds(continuation_token=None):
    url = f"https://dev.azure.com/{ORG}/{PROJECT}/_apis/build/builds?api-version=7.1&$top=100"
    if continuation_token:
        url += f"&continuationToken={continuation_token}"

    r = ado_get(url)
    return r.json(), r.headers.get("x-ms-continuationtoken")


def get_build(run_id):
    url = f"https://dev.azure.com/{ORG}/{PROJECT}/_apis/build/builds/{run_id}?api-version=7.1"
    return ado_get(url).json()


def get_timeline(run_id):
    url = f"https://dev.azure.com/{ORG}/{PROJECT}/_apis/build/builds/{run_id}/timeline?api-version=7.1"
    return ado_get(url).json()


def get_tests(run_id):
    url = f"https://dev.azure.com/{ORG}/{PROJECT}/_apis/test/runs?buildId={run_id}&api-version=7.1"
    try:
        return ado_get(url).json()
    except requests.exceptions.HTTPError as e:
        if e.response.status_code >= 500:
            print(f"  [TEST API 500] Ignoring tests for run {run_id}")
            return {"value": []}
        raise


# ================================================================
# FEATURE EXTRACTION
# ================================================================

def duration_seconds(start, finish):
    if not start or not finish:
        return 0
    s = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
    f = datetime.datetime.fromisoformat(finish.replace("Z", "+00:00"))
    return max(0, int((f - s).total_seconds()))


def classify_task(task_name: str):
    n = task_name.lower()
    if any(k in n for k in ["nuget restore", "dotnet restore", "npm install",
                              "yarn install", "pip install", "npm ci",
                              "restore packages", "package restore"]):
        return "dependency"
    if any(k in n for k in ["download artifact", "download pipeline artifact",
                              "download build artifact"]):
        return "download"
    if any(k in n for k in ["msbuild", "dotnet build", "dotnet publish",
                              "ng build", "webpack", "tsc", "javac",
                              "go build", "cargo build"]):
        return "compile"
    return None


# ================================================================
# CORE PROCESSOR
# ================================================================

def process_run(run_id, conn=None):
    print(f"\nProcessing run {run_id}...")
    owns_conn = conn is None
    if owns_conn:
        conn = sql_conn()
    cursor = conn.cursor()

    cursor.execute("SELECT 1 FROM PipelineRuns WHERE RunId = %s", (run_id,))
    if cursor.fetchone():
        print(f"  Run {run_id} already in DB — skipping.")
        cursor.close()
        if owns_conn:
            conn.close()
        return

    try:
        build = get_build(run_id)
        save_run_json(run_id, build)
    except Exception as e:
        print(f"  [SKIP] Failed to fetch build {run_id}: {e}")
        cursor.close()
        if owns_conn:
            conn.close()
        return

    # ---- PipelineRuns INSERT ----
    queue_time = build.get("queueTime")
    start  = build.get("startTime")
    finish = build.get("finishTime")
    duration = duration_seconds(start, finish)
    queue_wait = duration_seconds(queue_time, start)

    cursor.execute("""
        INSERT INTO PipelineRuns
        (ProjectName, RunId, PipelineId, PipelineName, Branch, CommitId,
         QueueTime, StartTime, FinishTime, TotalDurationSeconds,
         QueueWaitSeconds, Result, Reason, AgentPool)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        PROJECT,
        run_id,
        build["definition"]["id"],
        build["definition"]["name"],
        build.get("sourceBranch"),
        build.get("sourceVersion"),
        queue_time,
        start,
        finish,
        duration,
        queue_wait,
        build.get("result"),
        build.get("reason"),
        build.get("queue", {}).get("pool", {}).get("name"),
    ))

    # ---- Timeline ----
    try:
        timeline = get_timeline(run_id)
    except Exception as e:
        print(f"  [SKIP] Failed to fetch timeline for {run_id}: {e}")
        cursor.close()
        if owns_conn:
            conn.close()
        return

    records  = timeline.get("records", [])

    id_to_record = {r["id"]: r for r in records}

    def resolve_stage_and_job(rec):
        stage_name = "unknown"
        job_name = "unknown"

        current = rec

        while True:
            parent_id = current.get("parentId")
            if not parent_id:
                break

            parent = id_to_record.get(parent_id)
            if not parent:
                break

            ptype = parent.get("type")

            if ptype == "Job":
                job_name = parent.get("name", "unknown")

            elif ptype == "Stage":
                stage_name = parent.get("name", "unknown")

            current = parent

        return stage_name, job_name

    for rec in records:
        name   = rec.get("name")
        rtype  = rec.get("type")
        result = rec.get("result")
        dur    = duration_seconds(rec.get("startTime"), rec.get("finishTime"))

        if rtype == "Stage":
            cursor.execute(
                "INSERT INTO PipelineStages (RunId, StageName, Result, DurationSeconds) VALUES (%s,%s,%s,%s)",
                (run_id, name, result, dur),
            )
        if rtype == "Job":
            cursor.execute(
                "INSERT INTO PipelineJobs (RunId, JobName, Result, DurationSeconds) VALUES (%s,%s,%s,%s)",
                (run_id, name, result, dur),
            )

    # ---- Task processing with parallel-aware duration accounting ----
    task_records = [r for r in records if r.get("type") == "Task"]

    # Group by (stage, job) to correctly handle parallelism:
    #   Tasks within a job are sequential → sum durations
    #   Jobs within a stage are parallel  → take max
    #   Stages are sequential             → sum across stages
    stage_job_category = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))

    task_rows = []

    for rec in task_records:
        name     = rec.get("name", "unknown")
        result   = rec.get("result")
        dur      = duration_seconds(rec.get("startTime"), rec.get("finishTime"))
        category = classify_task(name)
        stage_name, job_name = resolve_stage_and_job(rec)

        if category:
            stage_job_category[stage_name][job_name][category] += dur

        task_rows.append((run_id, stage_name, job_name, name, result, dur))

    cursor.executemany("""
        INSERT INTO PipelineTasks
        (RunId, StageName, JobName, TaskName, Result, DurationSeconds)
        VALUES (%s,%s,%s,%s,%s,%s)
    """, task_rows)

    # Critical-path durations: max across parallel jobs per stage, then sum across stages
    dependency_seconds = download_seconds = compile_seconds = 0
    for stage, jobs in stage_job_category.items():
        dependency_seconds += max((j.get("dependency", 0) for j in jobs.values()), default=0)
        download_seconds   += max((j.get("download", 0) for j in jobs.values()), default=0)
        compile_seconds    += max((j.get("compile", 0) for j in jobs.values()), default=0)

    # Parallelism metrics
    job_records = [r for r in records if r.get("type") == "Job"]
    parallel_job_count = len(job_records)

    # Total compute time across all agents (sum of all task durations)
    total_timeline_seconds = sum(
        duration_seconds(r.get("startTime"), r.get("finishTime"))
        for r in records if r.get("type") == "Task"
    )

    cursor.execute("""
        INSERT INTO PipelineTimelineFeatures
        (RunId, DependencyRestoreSeconds, DownloadSeconds, CompileSeconds,
         TotalTimelineSeconds, ParallelJobCount)
        VALUES (%s,%s,%s,%s,%s,%s)
    """, (run_id, dependency_seconds, download_seconds, compile_seconds,
          total_timeline_seconds, parallel_job_count))

    # ---- PipelineTests INSERT ----
    try:
        tests  = get_tests(run_id)
    except Exception as e:
        print(f"  [WARN] Failed to fetch tests for {run_id}: {e}")
        tests = {"value": []}

    total  = passed = failed = skipped = 0
    for t in tests.get("value", []):
        total   += t.get("totalTests", 0)
        passed  += t.get("passedTests", 0)
        failed  += t.get("failedTests", 0)
        skipped += t.get("incompleteTests", 0)

    cursor.execute("""
        INSERT INTO PipelineTests
        (RunId, TotalTests, PassedTests, FailedTests, SkippedTests)
        VALUES (%s,%s,%s,%s,%s)
    """, (run_id, total, passed, failed, skipped))

    conn.commit()
    cursor.close()
    if owns_conn:
        conn.close()
    print(f"  Done: run {run_id}")

def backfill(max_runs=1000):
    collected = 0
    token = None
    conn = sql_conn()

    print(f"Starting backfill: target {max_runs} runs for project '{PROJECT}'")

    while collected < max_runs:
        data, token = get_builds(token)
        builds = data.get("value", [])

        if not builds:
            print("No more builds found.")
            break

        for b in builds:
            if collected >= max_runs:
                break
            conn = get_or_reconnect(conn)
            try:
                process_run(b["id"], conn=conn)
            except Exception as e:
                print(f"  [ERROR] Run {b['id']} failed with exception: {e}")
            collected += 1
            print(f"  Progress: {collected}/{max_runs}")

        if not token:
            print("Reached end of build history.")
            break

    conn.close()
    print(f"Backfill complete. Processed {collected} runs for project '{PROJECT}'.")


# ================================================================
# ENTRY POINT
# ================================================================

if __name__ == "__main__":
    backfill(MAX_RUNS)

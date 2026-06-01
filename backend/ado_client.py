from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import base64
import logging

import httpx

from config import settings
from database import get_conn

logger = logging.getLogger(__name__)

_ADO_API_VERSION = "7.1"


def _get_ado_headers(user_token: str | None = None) -> dict[str, str]:
    """Build PAT-based headers for ADO REST requests.

    user_token is ignored intentionally to keep this client fully PAT-based.
    """
    pat = settings.ADO_PAT.strip()
    if not pat:
        raise RuntimeError(
            "ADO_PAT is not configured. Set ADO_PAT in backend .env/App Settings."
        )
    basic = base64.b64encode(f":{pat}".encode("utf-8")).decode("ascii")
    return {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/json",
    }


def _duration_seconds(start: str | None, finish: str | None) -> int:
    if not start or not finish:
        return 0
    try:
        s = datetime.fromisoformat(start.replace("Z", "+00:00"))
        f = datetime.fromisoformat(finish.replace("Z", "+00:00"))
        return max(0, int((f - s).total_seconds()))
    except Exception:
        return 0


def _classify_task(name: str) -> str | None:
    n = (name or "").lower()
    if any(k in n for k in ["restore", "nuget", "npm", "yarn", "pnpm"]):
        return "dependency"
    if any(k in n for k in ["download", "checkout"]):
        return "download"
    if any(k in n for k in ["build", "compile", "msbuild", "dotnet build"]):
        return "compile"
    return None


def _extract_org_project(run_data: dict) -> tuple[str, str]:
    # Best-effort extraction from API URL, e.g.
    # https://dev.azure.com/{org}/{project}/_apis/pipelines/...
    url = str(run_data.get("url") or "")
    marker = "https://dev.azure.com/"
    if marker in url:
        tail = url.split(marker, 1)[1]
        parts = [p for p in tail.split("/") if p]
        if len(parts) >= 2:
            return parts[0], parts[1]
    project = str((run_data.get("project") or {}).get("name") or "")
    return "", project


def fetch_run_data(
    org:        str,
    project:    str,
    pipeline_id:int,
    run_id:     int,
    user_token: str,
) -> dict:
    """Fetch pipeline run metadata and timeline from Azure DevOps REST API.

    Returns a dict with keys:
      'run'      — dict from the Pipelines API (PipelineRun object)
      'timeline' — list of timeline records from the Build API
      'logs'     — {} (reserved for future use)

    Raises:
      RuntimeError if ADO_PAT is missing.
      httpx.HTTPStatusError if ADO returns a non-2xx response.
    """
    headers = _get_ado_headers(user_token)
    base = f"https://dev.azure.com/{org}/{project}"

    with httpx.Client(timeout=30) as client:
        # ── Pipeline run details ───────────────────────────────────────────────
        run_resp = client.get(
            f"{base}/_apis/pipelines/{pipeline_id}/runs/{run_id}",
            headers=headers,
            params={"api-version": _ADO_API_VERSION},
        )
        run_resp.raise_for_status()
        run_data = run_resp.json()

        # ── Build timeline (jobs, stages, tasks) ───────────────────────────────
        timeline_resp = client.get(
            f"{base}/_apis/build/builds/{run_id}/timeline",
            headers=headers,
            params={"api-version": _ADO_API_VERSION},
        )
        timeline_resp.raise_for_status()
        timeline_data = timeline_resp.json().get("records", [])

    return {
        "run":      run_data,
        "timeline": timeline_data,
        "logs":     {},
    }


# ── ADO browsing API (used by frontend org → project → pipeline → run flow) ────

def list_organizations(user_token: str) -> list[dict]:
    """Return the configured organization for PAT-based mode.

    PAT authentication is tied to a specific backend credential and does not
    reliably support user-profile account discovery endpoints.
    """
    org = settings.ADO_ORG.strip()
    if not org:
        raise RuntimeError("ADO_ORG is not configured.")

    return [
        {
            "id": org,
            "name": org,
            "url": f"https://dev.azure.com/{org}",
        }
    ]


def list_projects(org: str, user_token: str) -> list[dict]:
    """
    Return all projects in an ADO organization.

    Returns list of {"id": str, "name": str, "state": str, "description": str}.
    """
    headers = _get_ado_headers(user_token)

    with httpx.Client(timeout=15) as client:
        resp = client.get(
            f"https://dev.azure.com/{org}/_apis/projects",
            headers=headers,
            params={"api-version": _ADO_API_VERSION, "$top": 200},
        )
        resp.raise_for_status()
        projects = resp.json().get("value", [])

    return [
        {
            "id":          p["id"],
            "name":        p["name"],
            "state":       p.get("state", ""),
            "description": p.get("description", ""),
        }
        for p in projects
    ]


def list_pipelines(org: str, project: str, user_token: str) -> list[dict]:
    """
    Return all pipelines in an ADO project.

    Returns list of {"id": int, "name": str, "folder": str}.
    """
    headers = _get_ado_headers(user_token)

    with httpx.Client(timeout=15) as client:
        resp = client.get(
            f"https://dev.azure.com/{org}/{project}/_apis/pipelines",
            headers=headers,
            params={"api-version": _ADO_API_VERSION, "$top": 500},
        )
        resp.raise_for_status()
        pipelines = resp.json().get("value", [])

    return [
        {
            "id":     p["id"],
            "name":   p["name"],
            "folder": p.get("folder", "\\"),
        }
        for p in pipelines
    ]


def list_runs(
    org:         str,
    project:     str,
    pipeline_id: int,
    user_token:  str,
    top:         int = 50,
) -> list[dict]:
    """
    Return the most recent runs for a pipeline.

    Returns list of:
      {
        "id": int, "name": str, "state": str, "result": str | None,
        "created_date": str, "finished_date": str | None,
        "duration_seconds": int | None
      }
    """
    headers = _get_ado_headers(user_token)

    with httpx.Client(timeout=15) as client:
        resp = client.get(
            f"https://dev.azure.com/{org}/{project}/_apis/pipelines/{pipeline_id}/runs",
            headers=headers,
            params={"api-version": _ADO_API_VERSION, "$top": top},
        )
        resp.raise_for_status()
        runs = resp.json().get("value", [])

    result = []
    for r in runs:
        created  = r.get("createdDate")
        finished = r.get("finishedDate")
        duration = None
        if created and finished:
            from datetime import datetime as _dt
            try:
                c = _dt.fromisoformat(created.replace("Z", "+00:00"))
                f = _dt.fromisoformat(finished.replace("Z", "+00:00"))
                duration = max(0, int((f - c).total_seconds()))
            except Exception:
                pass

        result.append({
            "id":               r["id"],
            "name":             r.get("name", str(r["id"])),
            "state":            r.get("state", ""),
            "result":           r.get("result"),
            "created_date":     created,
            "finished_date":    finished,
            "duration_seconds": duration,
        })
    return result


def store_run_data(run_id: int, run_data: dict, timeline: list) -> None:
    """
    Persist ADO run + timeline records to the pipeline SQL tables.
    ─────────────────────────────────────────────────────────────────────────────
    """
    org_name, project_name = _extract_org_project(run_data)

    pipeline_obj = run_data.get("pipeline") or {}
    pipeline_id = int(pipeline_obj.get("id") or 0)
    pipeline_name = str(pipeline_obj.get("name") or "")

    resources = run_data.get("resources") or {}
    repo_self = (resources.get("repositories") or {}).get("self") or {}
    branch = str(repo_self.get("refName") or "")
    commit_id = str(repo_self.get("version") or run_data.get("sourceVersion") or "")

    queue_time = str(run_data.get("createdDate") or "")
    start_time = queue_time
    finish_time = str(run_data.get("finishedDate") or "")
    total_duration = _duration_seconds(start_time, finish_time)
    queue_wait = 0
    result = str(run_data.get("result") or "")
    reason = str(run_data.get("reason") or (run_data.get("trigger") or {}).get("reason") or "")
    agent_pool = str(((run_data.get("resources") or {}).get("queues") or {}).get("name") or "")

    records = timeline or []
    id_to_record = {r.get("id"): r for r in records if r.get("id")}

    def resolve_stage_and_job(rec: dict) -> tuple[str, str]:
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
                job_name = str(parent.get("name") or "unknown")
            elif ptype == "Stage":
                stage_name = str(parent.get("name") or "unknown")
            current = parent
        return stage_name, job_name

    with get_conn() as conn:
        cursor = conn.cursor()

        # PipelineRuns upsert
        existing = cursor.execute(
            "SELECT 1 FROM PipelineRuns WHERE RunId = ?",
            run_id,
        ).fetchone()

        if existing:
            cursor.execute(
                """
                UPDATE PipelineRuns
                SET ProjectName = ?,
                    PipelineId = ?,
                    PipelineName = ?,
                    Branch = ?,
                    CommitId = ?,
                    QueueTime = ?,
                    StartTime = ?,
                    FinishTime = ?,
                    TotalDurationSeconds = ?,
                    QueueWaitSeconds = ?,
                    Result = ?,
                    Reason = ?,
                    AgentPool = ?
                WHERE RunId = ?
                """,
                project_name,
                pipeline_id,
                pipeline_name,
                branch,
                commit_id,
                queue_time or None,
                start_time or None,
                finish_time or None,
                total_duration,
                queue_wait,
                result or None,
                reason or None,
                agent_pool or None,
                run_id,
            )
        else:
            cursor.execute(
                """
                INSERT INTO PipelineRuns
                (ProjectName, RunId, PipelineId, PipelineName, Branch, CommitId,
                 QueueTime, StartTime, FinishTime, TotalDurationSeconds,
                 QueueWaitSeconds, Result, Reason, AgentPool)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                project_name,
                run_id,
                pipeline_id,
                pipeline_name,
                branch,
                commit_id,
                queue_time or None,
                start_time or None,
                finish_time or None,
                total_duration,
                queue_wait,
                result or None,
                reason or None,
                agent_pool or None,
            )

        # Rebuild child data for idempotent re-runs
        cursor.execute("DELETE FROM PipelineTasks WHERE RunId = ?", run_id)
        cursor.execute("DELETE FROM PipelineJobs WHERE RunId = ?", run_id)
        cursor.execute("DELETE FROM PipelineStages WHERE RunId = ?", run_id)
        cursor.execute("DELETE FROM PipelineTimelineFeatures WHERE RunId = ?", run_id)
        cursor.execute("DELETE FROM PipelineTests WHERE RunId = ?", run_id)

        stage_rows: list[tuple] = []
        job_rows: list[tuple] = []
        task_rows: list[tuple] = []
        stage_job_category = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))

        for rec in records:
            rtype = rec.get("type")
            name = str(rec.get("name") or "unknown")
            rresult = rec.get("result")
            dur = _duration_seconds(rec.get("startTime"), rec.get("finishTime"))

            if rtype == "Stage":
                stage_rows.append((run_id, name, rresult, dur))
            elif rtype == "Job":
                job_rows.append((run_id, name, rresult, dur))
            elif rtype == "Task":
                stage_name, job_name = resolve_stage_and_job(rec)
                task_rows.append((run_id, stage_name, job_name, name, rresult, dur))

                category = _classify_task(name)
                if category:
                    stage_job_category[stage_name][job_name][category] += dur

        if stage_rows:
            cursor.executemany(
                """
                INSERT INTO PipelineStages (RunId, StageName, Result, DurationSeconds)
                VALUES (?, ?, ?, ?)
                """,
                stage_rows,
            )
        if job_rows:
            cursor.executemany(
                """
                INSERT INTO PipelineJobs (RunId, JobName, Result, DurationSeconds)
                VALUES (?, ?, ?, ?)
                """,
                job_rows,
            )
        if task_rows:
            cursor.executemany(
                """
                INSERT INTO PipelineTasks
                (RunId, StageName, JobName, TaskName, Result, DurationSeconds)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                task_rows,
            )

        dependency_seconds = 0
        download_seconds = 0
        compile_seconds = 0
        for _, jobs in stage_job_category.items():
            dependency_seconds += max((j.get("dependency", 0) for j in jobs.values()), default=0)
            download_seconds += max((j.get("download", 0) for j in jobs.values()), default=0)
            compile_seconds += max((j.get("compile", 0) for j in jobs.values()), default=0)

        parallel_job_count = len(job_rows)
        total_timeline_seconds = sum(r[5] for r in task_rows)

        cursor.execute(
            """
            INSERT INTO PipelineTimelineFeatures
            (RunId, DependencyRestoreSeconds, DownloadSeconds, CompileSeconds,
             TotalTimelineSeconds, ParallelJobCount)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            run_id,
            dependency_seconds,
            download_seconds,
            compile_seconds,
            total_timeline_seconds,
            parallel_job_count,
        )

        # test summary is fetched by backfill in full mode; here we persist safe defaults
        cursor.execute(
            """
            INSERT INTO PipelineTests
            (RunId, TotalTests, PassedTests, FailedTests, SkippedTests)
            VALUES (?, ?, ?, ?, ?)
            """,
            run_id,
            0,
            0,
            0,
            0,
        )

        conn.commit()

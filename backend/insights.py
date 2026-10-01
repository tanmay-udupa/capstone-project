from __future__ import annotations

import json
import logging
from datetime import timedelta
from functools import lru_cache
from urllib.parse import quote

import pandas as pd
import pyodbc

from config import settings
from database import get_conn

logger = logging.getLogger(__name__)

LATE_FAILURE_MINUTES = 30
# Longer than a holiday shutdown, so a silence this long is reported as missing data.
MIN_GAP_DAYS = 14
_PASSED_RESULTS = ("succeeded", "partiallySucceeded")
_UNSUCCESSFUL_RESULTS = ("failed", "canceled")
_UNKNOWN = "unknown"
_DETAIL_ROWS = 5

# Agent time is approximated by job duration, so agentless jobs (approvals, waits) are included.
_RUNS_SQL = """
SELECT pr.RunId,
       pr.ProjectName,
       pr.PipelineName,
       pr.Branch,
       pr.CommitId,
       pr.Result,
       pr.Reason,
       pr.AgentPool,
       pr.TotalDurationSeconds,
       CONVERT(datetime2(0), TRY_CONVERT(datetimeoffset, pr.StartTime)) AS StartedAt,
       COALESCE(j.AgentSeconds, 0)                                        AS AgentSeconds
FROM PipelineRuns pr
LEFT JOIN (
    SELECT RunId, SUM(DurationSeconds) AS AgentSeconds
    FROM PipelineJobs
    GROUP BY RunId
) j ON j.RunId = pr.RunId
WHERE pr.FinishTime IS NOT NULL
"""

_PROJECTS_SQL = """
SELECT ProjectName, COUNT(*) AS Runs
FROM PipelineRuns
WHERE FinishTime IS NOT NULL
GROUP BY ProjectName
"""

# The longest execution of a task within a run counts as needed; later ones are repeats.
# The () grouping set adds one grand-total row (IsTotal = 1).
_TASK_STATS_SQL = """
WITH scoped_runs AS (
    SELECT CAST([value] AS bigint) AS RunId FROM OPENJSON(CAST(? AS nvarchar(max)))
),
tasks AS (
    SELECT pr.PipelineName,
           pt.RunId,
           pt.TaskName,
           pt.DurationSeconds,
           ROW_NUMBER() OVER (
               PARTITION BY pt.RunId, pt.TaskName
               ORDER BY pt.DurationSeconds DESC
           ) AS Occurrence
    FROM PipelineTasks pt
    JOIN scoped_runs s   ON s.RunId  = pt.RunId
    JOIN PipelineRuns pr ON pr.RunId = pt.RunId
    WHERE pt.TaskName NOT IN ('Initialize job', 'Finalize Job')
      AND pt.TaskName NOT LIKE 'Pre-job:%'
      AND pt.TaskName NOT LIKE 'Post-job:%'
)
SELECT PipelineName,
       TaskName,
       GROUPING(TaskName)                                            AS IsTotal,
       COUNT(DISTINCT RunId)                                         AS Runs,
       COUNT(*)                                                      AS Occurrences,
       SUM(DurationSeconds)                                          AS AgentSeconds,
       COUNT(DISTINCT CASE WHEN Occurrence > 1 THEN RunId END)       AS RunsWithRepeats,
       SUM(CASE WHEN Occurrence > 1 THEN 1 ELSE 0 END)               AS ExtraOccurrences,
       SUM(CASE WHEN Occurrence > 1 THEN DurationSeconds ELSE 0 END) AS RepeatedSeconds
FROM tasks
GROUP BY GROUPING SETS ((PipelineName, TaskName), ())
"""

_TASK_NUMERIC_COLUMNS = (
    "IsTotal",
    "Runs",
    "Occurrences",
    "AgentSeconds",
    "RunsWithRepeats",
    "ExtraOccurrences",
    "RepeatedSeconds",
)

# DISTINCT: a task that failed in several parallel jobs counts once per run.
_FAILED_TASKS_SQL = """
WITH scoped_runs AS (
    SELECT CAST([value] AS bigint) AS RunId FROM OPENJSON(CAST(? AS nvarchar(max)))
)
SELECT DISTINCT pt.RunId, pt.TaskName
FROM PipelineTasks pt
JOIN scoped_runs s ON s.RunId = pt.RunId
WHERE pt.Result = 'failed'
"""

# Changes whenever a run is stored or rewritten, which is what makes a cached result out of date.
_DATA_VERSION_SQL = """
SELECT COUNT_BIG(*),
       MAX(RunId),
       CHECKSUM_AGG(CHECKSUM(RunId, ProjectName, PipelineName, Branch, CommitId, Result, Reason,
                             AgentPool, TotalDurationSeconds, StartTime, FinishTime))
FROM PipelineRuns
"""


# ── Public API ─────────────────────────────────────────────────────────────────

def get_insights(project: str | None = None, days: int | None = None, top_n: int = 10) -> dict:
    """Summarise where CI compute went across stored runs. Cached, so don't modify the returned dict."""
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(_DATA_VERSION_SQL)
        data_version = tuple(cursor.fetchone())
    return _compute_insights(project, days, top_n, data_version)


@lru_cache(maxsize=32)
def _compute_insights(project: str | None, days: int | None, top_n: int, _data_version: tuple) -> dict:
    with get_conn() as conn:
        all_runs = _prepare_runs(_fetch_frame(conn, _RUNS_SQL))
        runs = _select_scope(all_runs, project, days)
        run_ids = json.dumps([int(run_id) for run_id in runs["RunId"]])
        tasks = _prepare_tasks(_fetch_frame(conn, _TASK_STATS_SQL, run_ids))
        failed_ids = json.dumps([int(run_id) for run_id in runs.loc[runs["Result"] == "failed", "RunId"]])
        failed_tasks = _prepare_failed_tasks(_fetch_frame(conn, _FAILED_TASKS_SQL, failed_ids))

    logger.info("Insights computed: project=%s days=%s runs=%d", project, days, len(runs))
    return summarise(runs, tasks, failed_tasks, project, days, top_n)


def get_projects() -> list[dict]:
    """Projects that have stored, completed runs, most runs first."""
    with get_conn() as conn:
        return _projects(_fetch_frame(conn, _PROJECTS_SQL))


def summarise(
    runs:         pd.DataFrame,
    tasks:        pd.DataFrame,
    failed_tasks: pd.DataFrame,
    project:      str | None,
    days:         int | None,
    top_n:        int,
) -> dict:
    """Build the insights payload from prepared run and task frames."""
    task_rows   = tasks[tasks["IsTotal"] == 0]
    task_totals = tasks[tasks["IsTotal"] == 1]

    return {
        "scope":          _scope(runs, project, days),
        "by_outcome":     _breakdown(runs, "Result"),
        "by_pool":        _breakdown(runs, "AgentPool"),
        "waste":          _waste(
            runs,
            failed_tasks,
            repeated_runs=int(task_totals["RunsWithRepeats"].sum()),
            repeated_seconds=int(task_totals["RepeatedSeconds"].sum()),
        ),
        "top_pipelines":  _top_pipelines(runs, top_n),
        "top_tasks":      _top_tasks(task_rows, top_n),
        "repeated_tasks": _repeated_tasks(task_rows, top_n),
    }


# ── Loading ────────────────────────────────────────────────────────────────────

def _fetch_frame(conn: pyodbc.Connection, sql: str, *params) -> pd.DataFrame:
    cursor = conn.cursor()
    cursor.execute(sql, *params)
    columns = [column[0] for column in cursor.description]
    return pd.DataFrame.from_records([tuple(row) for row in cursor.fetchall()], columns=columns)


def _label(values: pd.Series) -> pd.Series:
    return values.fillna("").astype(str).str.strip().replace("", _UNKNOWN)


def _prepare_runs(runs: pd.DataFrame) -> pd.DataFrame:
    runs = runs.copy()
    runs["StartedAt"] = pd.to_datetime(runs["StartedAt"], errors="coerce")
    for column in ("AgentSeconds", "TotalDurationSeconds"):
        runs[column] = pd.to_numeric(runs[column], errors="coerce").fillna(0).astype("int64")
    for column in ("ProjectName", "PipelineName", "Result", "AgentPool"):
        runs[column] = _label(runs[column])
    runs["CommitId"] = runs["CommitId"].fillna("").astype(str).str.strip()
    return runs


def _prepare_tasks(tasks: pd.DataFrame) -> pd.DataFrame:
    tasks = tasks.copy()
    for column in _TASK_NUMERIC_COLUMNS:
        tasks[column] = pd.to_numeric(tasks[column], errors="coerce").fillna(0).astype("int64")
    for column in ("PipelineName", "TaskName"):
        tasks[column] = _label(tasks[column])
    return tasks


def _prepare_failed_tasks(failed_tasks: pd.DataFrame) -> pd.DataFrame:
    return failed_tasks.assign(
        RunId=pd.to_numeric(failed_tasks["RunId"], errors="coerce").fillna(0).astype("int64"),
        TaskName=_label(failed_tasks["TaskName"]),
    )


def _select_scope(runs: pd.DataFrame, project: str | None, days: int | None) -> pd.DataFrame:
    if project:
        runs = runs[runs["ProjectName"] == project]
    # The window ends at the latest stored run rather than today, so older backfills still return data.
    latest = runs["StartedAt"].max()
    if days and pd.notna(latest):
        runs = runs[runs["StartedAt"] >= latest - timedelta(days=days)]
    return runs


# ── Aggregations ───────────────────────────────────────────────────────────────

def _iso(value: pd.Timestamp) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if pd.notna(value) else None


def _scope(runs: pd.DataFrame, project: str | None, days: int | None) -> dict:
    first, last = runs["StartedAt"].min(), runs["StartedAt"].max()
    has_dates = pd.notna(first) and pd.notna(last)
    incomplete_before = _incomplete_before(runs["StartedAt"], days) if has_dates else None
    recent = runs if incomplete_before is None else runs[runs["StartedAt"] >= incomplete_before]
    recent_days = (last - recent["StartedAt"].min()).total_seconds() / 86400 if has_dates else 0
    recent_seconds = int(recent["AgentSeconds"].sum())
    return {
        "project":                   project,
        "days":                      days,
        "runs":                      len(runs),
        "pipelines":                 int(runs["PipelineName"].nunique()),
        "first_run_utc":             _iso(first),
        "last_run_utc":              _iso(last),
        "span_days":                 max(1, _whole_days(last - first)) if has_dates else 0,
        "agent_seconds":             int(runs["AgentSeconds"].sum()),
        "incomplete_before_utc":     _iso(incomplete_before),
        "agent_seconds_per_30_days": round(recent_seconds * 30 / recent_days) if recent_days >= 7 else None,
    }


def _incomplete_before(started: pd.Series, days: int | None) -> pd.Timestamp | None:
    """First run after the latest silence of MIN_GAP_DAYS or more (an empty start of the period counts)."""
    times = started.dropna().sort_values()
    after_gap = times[times.diff() >= timedelta(days=MIN_GAP_DAYS)]
    if not after_gap.empty:
        return after_gap.iloc[-1]
    if days and times.iloc[0] - (times.iloc[-1] - timedelta(days=days)) >= timedelta(days=MIN_GAP_DAYS):
        return times.iloc[0]
    return None


def _whole_days(delta: timedelta) -> int:
    return round(delta.total_seconds() / 86400)


def _breakdown(runs: pd.DataFrame, column: str) -> list[dict]:
    grouped = (
        runs.groupby(column)
        .agg(runs=("RunId", "size"), agent_seconds=("AgentSeconds", "sum"))
        .sort_values("agent_seconds", ascending=False)
    )
    return [
        {"name": str(row.Index), "runs": int(row.runs), "agent_seconds": int(row.agent_seconds)}
        for row in grouped.itertuples()
    ]


def _category(
    key:              str,
    title:            str,
    subset:           pd.DataFrame,
    failed_tasks:     pd.DataFrame,
    *,
    likely_avoidable: bool,
    fix:              str,
    related_label:    str | None = None,
) -> dict:
    return {
        "key":              key,
        "title":            title,
        "runs":             len(subset),
        "agent_seconds":    int(subset["AgentSeconds"].sum()),
        "likely_avoidable": likely_avoidable,
        "fix":              fix,
        "pipelines":        _breakdown(subset, "PipelineName")[:_DETAIL_ROWS],
        "examples":         _example_runs(subset),
        "related_label":    related_label,
        "failed_tasks":     _failed_task_counts(subset, failed_tasks),
    }


def _waste(
    runs:             pd.DataFrame,
    failed_tasks:     pd.DataFrame,
    *,
    repeated_runs:    int,
    repeated_seconds: int,
) -> list[dict]:
    has_commit = runs["CommitId"] != ""

    scheduled = runs[has_commit & (runs["Reason"] == "schedule")].sort_values("RunId")
    previous = scheduled.groupby(["PipelineName", "Branch"], dropna=False)[["CommitId", "Result", "RunId"]].shift()
    rebuilt = scheduled["CommitId"] == previous["CommitId"]
    scheduled = _with_related(scheduled, previous["RunId"], runs)

    other_triggers = runs[has_commit & (runs["Reason"] != "schedule")].sort_values("RunId")
    rerun_of = other_triggers.groupby(["PipelineName", "CommitId"])["RunId"].shift()
    reruns = _with_related(other_triggers[rerun_of.notna()], rerun_of, runs)

    failed = runs["Result"] == "failed"
    next_pass = _next_passing_run(runs)
    passed_on_rerun = failed & has_commit & next_pass.notna()
    late = failed & (runs["TotalDurationSeconds"] >= _late_failure_seconds(runs))

    categories = [
        _category(
            "scheduled_rebuild_passed",
            "Scheduled runs rebuilding a commit that already passed",
            scheduled[rebuilt & previous["Result"].isin(_PASSED_RESULTS)],
            failed_tasks,
            likely_avoidable=True,
            fix="Only run the schedule when the code has changed (remove always: true).",
            related_label="Same commit as",
        ),
        _category(
            "scheduled_rebuild_failed",
            "Scheduled runs rebuilding a commit that already failed",
            scheduled[rebuilt & (previous["Result"] == "failed")],
            failed_tasks,
            likely_avoidable=True,
            fix="Fix or pause the failing build instead of rebuilding a commit that is known to fail.",
            related_label="Same commit as",
        ),
        _category(
            "failed_then_passed",
            "Failed runs whose commit passed when re-run",
            _with_related(runs[passed_on_rerun], next_pass, runs),
            failed_tasks,
            likely_avoidable=True,
            fix="Likely flaky tests or infrastructure: fix or quarantine the task that failed.",
            related_label="Passed in",
        ),
        _category(
            "late_failures",
            f"Runs that failed after {LATE_FAILURE_MINUTES}+ minutes and half their usual run time",
            runs[late & ~passed_on_rerun],
            failed_tasks,
            likely_avoidable=False,
            fix="Fail fast: run quick checks first and stop at the first failure.",
        ),
        _category(
            "canceled",
            "Canceled runs",
            runs[runs["Result"] == "canceled"],
            failed_tasks,
            likely_avoidable=False,
            fix="Find out why runs are canceled; batch CI triggers and cancel superseded runs early.",
        ),
        _category(
            "same_commit_reruns",
            "Repeat runs of the same commit",
            reruns,
            failed_tasks,
            likely_avoidable=False,
            fix="Fix the flaky tasks or infrastructure behind manual retries, and retry only the failed stage.",
            related_label="Re-run of",
        ),
        {
            "key":              "repeated_tasks",
            "title":            "Repeated tasks within a run",
            "runs":             repeated_runs,
            "agent_seconds":    repeated_seconds,
            "likely_avoidable": False,
            "fix":              (
                "Cache restores (Cache@2), build once and publish a pipeline artifact, "
                "and use checkout: none in jobs that only need artifacts."
            ),
            # The repeated tasks list already breaks this down per task.
            "pipelines":        [],
            "examples":         [],
            "related_label":    None,
            "failed_tasks":     [],
        },
    ]
    return sorted(categories, key=lambda c: (not c["likely_avoidable"], -c["agent_seconds"]))


def _next_passing_run(runs: pd.DataFrame) -> pd.Series:
    """First later passing RunId of the same pipeline and commit (NaN when there is none)."""
    ordered = runs.sort_values("RunId")
    keys = [ordered["PipelineName"], ordered["CommitId"]]
    passing = ordered["RunId"].where(ordered["Result"].isin(_PASSED_RESULTS))
    return passing.groupby(keys).shift(-1).groupby(keys).bfill().reindex(runs.index)


def _with_related(subset: pd.DataFrame, related_ids: pd.Series, runs: pd.DataFrame) -> pd.DataFrame:
    by_id = runs.set_index("RunId")
    return subset.assign(
        RelatedRunId=related_ids,
        RelatedResult=related_ids.map(by_id["Result"]),
        RelatedProject=related_ids.map(by_id["ProjectName"]),
    )


def _late_failure_seconds(runs: pd.DataFrame) -> pd.Series:
    """Half the pipeline's median passing run time, but at least LATE_FAILURE_MINUTES."""
    passing = runs["TotalDurationSeconds"].where(runs["Result"].isin(_PASSED_RESULTS))
    half_typical = passing.groupby(runs["PipelineName"]).transform("median") / 2
    return half_typical.fillna(0).clip(lower=LATE_FAILURE_MINUTES * 60)


def _example_runs(subset: pd.DataFrame) -> list[dict]:
    return [
        {
            "run_id":        int(row.RunId),
            "pipeline":      row.PipelineName,
            "started_utc":   _iso(row.StartedAt),
            "agent_seconds": int(row.AgentSeconds),
            "url":           _run_url(row.ProjectName, int(row.RunId)),
            "related":       _related_run(row),
        }
        for row in subset.nlargest(_DETAIL_ROWS, "AgentSeconds").itertuples(index=False)
    ]


def _related_run(row: tuple) -> dict | None:
    related_id = getattr(row, "RelatedRunId", None)
    if pd.isna(related_id):
        return None
    return {
        "run_id": int(related_id),
        "result": row.RelatedResult,
        "url":    _run_url(row.RelatedProject, int(related_id)),
    }


def _failed_task_counts(subset: pd.DataFrame, failed_tasks: pd.DataFrame) -> list[dict]:
    pipeline_by_run = dict(zip(subset["RunId"], subset["PipelineName"]))
    matched = failed_tasks[failed_tasks["RunId"].isin(subset["RunId"])]
    counts = (
        matched.assign(PipelineName=matched["RunId"].map(pipeline_by_run))
        .value_counts(["PipelineName", "TaskName"])
        .head(_DETAIL_ROWS)
    )
    return [{"pipeline": pipeline, "task": task, "runs": int(count)} for (pipeline, task), count in counts.items()]


def _run_url(project: str, run_id: int) -> str | None:
    if project == _UNKNOWN:
        return None
    org, project = quote(settings.ADO_ORG, safe=""), quote(project, safe="")
    return f"https://dev.azure.com/{org}/{project}/_build/results?buildId={run_id}"


def _top_pipelines(runs: pd.DataFrame, top_n: int) -> list[dict]:
    unsuccessful = runs["AgentSeconds"].where(runs["Result"].isin(_UNSUCCESSFUL_RESULTS), 0)
    grouped = (
        runs.assign(Unsuccessful=unsuccessful)
        .groupby("PipelineName")
        .agg(
            runs=("RunId", "size"),
            agent_seconds=("AgentSeconds", "sum"),
            unsuccessful=("Unsuccessful", "sum"),
        )
        .sort_values("agent_seconds", ascending=False)
        .head(top_n)
    )
    return [
        {
            "pipeline":                   str(row.Index),
            "runs":                       int(row.runs),
            "agent_seconds":              int(row.agent_seconds),
            "failed_or_canceled_seconds": int(row.unsuccessful),
        }
        for row in grouped.itertuples()
    ]


def _top_tasks(task_rows: pd.DataFrame, top_n: int) -> list[dict]:
    top = task_rows.sort_values("AgentSeconds", ascending=False).head(top_n)
    return [
        {
            "pipeline":      row.PipelineName,
            "task":          row.TaskName,
            "runs":          int(row.Runs),
            "occurrences":   int(row.Occurrences),
            "agent_seconds": int(row.AgentSeconds),
        }
        for row in top.itertuples(index=False)
    ]


def _repeated_tasks(task_rows: pd.DataFrame, top_n: int) -> list[dict]:
    top = (
        task_rows[task_rows["RepeatedSeconds"] > 0]
        .sort_values("RepeatedSeconds", ascending=False)
        .head(top_n)
    )
    return [
        {
            "pipeline":          row.PipelineName,
            "task":              row.TaskName,
            "runs":              int(row.RunsWithRepeats),
            "extra_occurrences": int(row.ExtraOccurrences),
            "repeated_seconds":  int(row.RepeatedSeconds),
        }
        for row in top.itertuples(index=False)
    ]


def _projects(counts: pd.DataFrame) -> list[dict]:
    runs = pd.to_numeric(counts["Runs"], errors="coerce").fillna(0)
    # Labelled like _prepare_runs, so a picked name matches the run filter.
    totals = runs.groupby(_label(counts["ProjectName"])).sum().sort_values(ascending=False)
    return [{"name": str(name), "runs": int(total)} for name, total in totals.items()]

from __future__ import annotations

import json
from datetime import datetime, timezone

import pyodbc

from config import settings

_CONN_STR = (
    f"DRIVER={{ODBC Driver 18 for SQL Server}};"
    f"SERVER={settings.SQL_SERVER};"
    f"DATABASE={settings.SQL_DATABASE};"
    f"UID={settings.SQL_USERNAME};"
    f"PWD={settings.SQL_PASSWORD};"
    "Encrypt=yes;TrustServerCertificate=no;"
)


def get_conn() -> pyodbc.Connection:
    """Open a new pyodbc connection. Use as a context manager for auto-commit/close."""
    return pyodbc.connect(_CONN_STR)


def ping_db() -> bool:
    """Lightweight liveness check used by /v1/health."""
    try:
        with get_conn() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


# ── AnalysisResults CRUD ───────────────────────────────────────────────────────

def create_analysis(
    org:             str,
    project:         str,
    pipeline_id:     int,
    run_id:          int,
    requested_by:    str,
        request_payload: dict,
) -> tuple[int, bool]:
    """Insert a new pending analysis row unless this run already has an analysis.

    Returns:
        (analysis_id, created_new)
    """
    existing = get_latest_analysis_by_run(run_id)
    if existing:
        return int(existing["analysis_id"]), False

    with get_conn() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO AnalysisResults
                    (OrgName, ProjectName, PipelineId, RunId, Status,
                     RequestedBy, RequestPayload)
                OUTPUT INSERTED.AnalysisId
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                org,
                project,
                pipeline_id,
                run_id,
                requested_by,
                json.dumps(request_payload),
            )
            row = cursor.fetchone()
            conn.commit()
            return int(row[0]), True
        except pyodbc.IntegrityError:
            # Concurrency-safe fallback in case a uniqueness rule is added in SQL.
            existing = get_latest_analysis_by_run(run_id)
            if existing:
                return int(existing["analysis_id"]), False
            raise


def update_analysis(
    analysis_id: int,
    status:      str,
    result:      dict | None = None,
    error:       str | None  = None,
) -> None:
    """Update status, result JSON, error message, and CompletedAt timestamp."""
    completed_at = (
        datetime.now(timezone.utc)
        if status in ("complete", "failed")
        else None
    )
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE AnalysisResults
            SET Status       = ?,
                ResultJson   = ?,
                ErrorMessage = ?,
                CompletedAt  = ?
            WHERE AnalysisId = ?
            """,
            status,
            json.dumps(result) if result is not None else None,
            error,
            completed_at,
            analysis_id,
        )
        conn.commit()


def get_analysis(analysis_id: int) -> dict | None:
    """Return analysis row as dict, or None if not found."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT AnalysisId, OrgName, ProjectName, PipelineId, RunId,
                   Status, ResultJson, ErrorMessage, StartedAt, CompletedAt,
                   RequestPayload
            FROM   AnalysisResults
            WHERE  AnalysisId = ?
            """,
            analysis_id,
        ).fetchone()

    if not row:
        return None

    return {
        "analysis_id":     int(row[0]),
        "org":             row[1],
        "project":         row[2],
        "pipeline_id":     int(row[3]),
        "run_id":          int(row[4]),
        "status":          row[5],
        "result":          json.loads(row[6]) if row[6] else None,
        "error_message":   row[7],
        "started_at":      row[8].isoformat() if row[8] else None,
        "completed_at":    row[9].isoformat() if row[9] else None,
        "request_payload": json.loads(row[10]) if row[10] else None,
    }


def get_latest_analysis_by_run(run_id: int) -> dict | None:
    """Return latest analysis row for a run, or None if not found."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT TOP 1 AnalysisId, Status
            FROM AnalysisResults
            WHERE RunId = ?
            ORDER BY AnalysisId DESC
            """,
            run_id,
        ).fetchone()

    if not row:
        return None

    return {
        "analysis_id": int(row[0]),
        "status": str(row[1]),
    }


# ── PipelineRuns helpers ───────────────────────────────────────────────────────

def run_exists(run_id: int) -> bool:
    """Return True if this RunId is already stored in PipelineRuns."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM PipelineRuns WHERE RunId = ?", run_id
        ).fetchone()
    return row is not None


def get_pipeline_name(run_id: int) -> str | None:
    """Return PipelineName for this run, or None if not found."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT PipelineName FROM PipelineRuns WHERE RunId = ?", run_id
        ).fetchone()
    return str(row[0]) if row else None


def get_project_name(run_id: int) -> str | None:
    """Return ProjectName for this run, or None if not found."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT ProjectName FROM PipelineRuns WHERE RunId = ?", run_id
        ).fetchone()
    return str(row[0]) if row else None

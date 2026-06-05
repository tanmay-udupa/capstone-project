from __future__ import annotations

import pandas as pd

from database import get_conn

# ── Locked feature order — matches X_train.csv header exactly ─────────────────
MODEL_FEATURES: list[str] = [
    "queue_wait_seconds",
    "result_failed",
    "result_partial",
    "is_scheduled",
    "is_main_branch",
    "job_count",
    "failed_job_count",
    "max_job_duration_seconds",
    "avg_job_duration_seconds",
    "stage_count",
    "failed_stage_count",
    "task_count",
    "failed_task_count",
    "skipped_task_count",
    "restore_task_seconds",
    "download_task_seconds",
    "build_task_seconds",
    "test_task_seconds",
    "deploy_task_seconds",
    "security_scan_seconds",
    "firewall_overhead_seconds",
    "unique_task_count",
    "duplicate_task_occurrences",
    "total_tests",
    "failed_test_count",
    "skipped_test_count",
    "test_pass_rate",
    "dependency_restore_seconds",
    "download_seconds",
    "compile_seconds",
    "parallel_job_count",
    "total_timeline_seconds",
    "parallelism_ratio",
    "has_parallel_execution",
    "has_test_results",
]

# ── Phase → feature column map (used by benchmark and recommender) ─────────────
PHASE_FEATURES: dict[str, str] = {
    "queue":         "queue_wait_seconds",
    "restore":       "restore_task_seconds",
    "download":      "download_task_seconds",
    "build":         "build_task_seconds",
    "test":          "test_task_seconds",
    "deploy":        "deploy_task_seconds",
    "security_scan": "security_scan_seconds",
    "firewall":      "firewall_overhead_seconds",
}

# ── Feature extraction SQL — Week 3 logic, single-run variant ─────────────────
# Parameters (7 × run_id): job WHERE, stage WHERE,
#   task dup-inner WHERE, task outer WHERE, test WHERE, main WHERE
_FEATURE_SQL = """
WITH job_features AS (
    SELECT
        RunId,
        COUNT(*)                                                        AS job_count,
        SUM(CASE WHEN Result = 'failed' THEN 1 ELSE 0 END)             AS failed_job_count,
        MAX(DurationSeconds)                                            AS max_job_duration_seconds,
        AVG(DurationSeconds * 1.0)                                      AS avg_job_duration_seconds
    FROM PipelineJobs
    WHERE RunId = ?
    GROUP BY RunId
),

stage_features AS (
    SELECT
        RunId,
        COUNT(*)                                                        AS stage_count,
        SUM(CASE WHEN Result = 'failed' THEN 1 ELSE 0 END)             AS failed_stage_count
    FROM PipelineStages
    WHERE RunId = ?
    GROUP BY RunId
),

task_features AS (
    SELECT
        pt.RunId,
        COUNT(*)                                                        AS task_count,
        SUM(CASE WHEN pt.Result = 'failed'  THEN 1 ELSE 0 END)         AS failed_task_count,
        SUM(CASE WHEN pt.Result = 'skipped' THEN 1 ELSE 0 END)         AS skipped_task_count,

        -- RESTORE / PACKAGE
        SUM(CASE WHEN (
                pt.TaskName LIKE '%NuGet%'
            OR  pt.TaskName LIKE 'Install PnDModules'
            OR  pt.TaskName LIKE '%npm%'
            OR  pt.TaskName LIKE '%Restore%'
            OR  pt.TaskName LIKE 'Install node%'
            OR  pt.TaskName LIKE 'Install npm%'
            ) AND pt.TaskName NOT LIKE 'Pre-job:%'
              AND pt.TaskName NOT LIKE 'Post-job:%'
            THEN pt.DurationSeconds ELSE 0 END)                         AS restore_task_seconds,

        -- DOWNLOAD / CHECKOUT
        SUM(CASE WHEN (
                pt.TaskName LIKE '%Download%'
            OR  pt.TaskName LIKE '%Checkout%'
            ) AND pt.TaskName NOT LIKE 'Pre-job:%'
              AND pt.TaskName NOT LIKE 'Post-job:%'
            THEN pt.DurationSeconds ELSE 0 END)                         AS download_task_seconds,

        -- BUILD / COMPILE
        SUM(CASE WHEN (
                pt.TaskName LIKE '%Build%'
            OR  pt.TaskName LIKE '%Compile%'
            OR  pt.TaskName LIKE '%compile%'
            OR  pt.TaskName LIKE '%MSBuild%'
            OR  pt.TaskName LIKE '%dotnet build%'
            OR  pt.TaskName LIKE '%DotNet Build%'
            ) AND pt.TaskName NOT LIKE 'Pre-job:%'
              AND pt.TaskName NOT LIKE 'Post-job:%'
            THEN pt.DurationSeconds ELSE 0 END)                         AS build_task_seconds,

        -- TEST
        SUM(CASE WHEN (
                pt.TaskName LIKE '%Test%'
            OR  pt.TaskName LIKE '%Tests%'
            OR  pt.TaskName LIKE '%Playwright%'
            OR  pt.TaskName LIKE '%Cypress%'
            OR  pt.TaskName LIKE '%VSTest%'
            OR  pt.TaskName LIKE '%Jest%'
            OR  pt.TaskName LIKE '%SilkTest%'
            ) AND pt.TaskName NOT LIKE 'Pre-job:%'
              AND pt.TaskName NOT LIKE 'Post-job:%'
            THEN pt.DurationSeconds ELSE 0 END)                         AS test_task_seconds,

        -- DEPLOY / PUBLISH
        SUM(CASE WHEN (
                pt.TaskName LIKE '%Deploy%'
            OR  pt.TaskName LIKE '%Deploying%'
            OR  pt.TaskName LIKE '%Publish%'
            OR  pt.TaskName LIKE '%publish%'
            OR  pt.TaskName LIKE 'Deploy Helm%'
            OR  pt.TaskName LIKE 'AzureWebApp'
            ) AND pt.TaskName NOT LIKE 'Pre-job:%'
              AND pt.TaskName NOT LIKE 'Post-job:%'
            THEN pt.DurationSeconds ELSE 0 END)                         AS deploy_task_seconds,

        -- SECURITY SCANS
        SUM(CASE WHEN (
                pt.TaskName  LIKE '%Black%'         OR pt.TaskName  LIKE '%Polaris%'
            OR  pt.TaskName  LIKE '%BinSkim%'       OR pt.TaskName  LIKE '%Malware%'
            OR  pt.TaskName  LIKE '%Defender%'      OR pt.TaskName  LIKE '%Coverity%'
            OR  pt.TaskName  LIKE '%CredScan%'      OR pt.TaskName  LIKE '%SDL%'
            OR  pt.TaskName  LIKE '%Synopsys%'      OR pt.TaskName  LIKE '%Whitesource%'
            OR  pt.TaskName  LIKE '%AntiMalware%'
            OR  pt.JobName   LIKE '%Security%'      OR pt.JobName   LIKE '%Polaris%'
            OR  pt.JobName   LIKE '%SDL%'           OR pt.JobName   LIKE '%Compliance%'
            OR  pt.JobName   LIKE '%Coverity%'      OR pt.JobName   LIKE '%Scan%'
            OR  pt.JobName   LIKE '%Synopsys%'      OR pt.JobName   LIKE '%Black%'
            OR  pt.StageName LIKE '%Security%'      OR pt.StageName LIKE '%SDL%'
            OR  pt.StageName LIKE '%Compliance%'    OR pt.StageName LIKE '%Polaris%'
            OR  pt.StageName LIKE '%Scan%'          OR pt.StageName LIKE '%Coverity%'
            OR  pt.StageName LIKE '%Black%'
            ) AND pt.TaskName NOT IN ('Initialize job', 'Finalize Job')
              AND pt.TaskName NOT LIKE 'Pre-job:%'
              AND pt.TaskName NOT LIKE 'Post-job:%'
              AND pt.TaskName NOT LIKE 'Microsoft Defender for DevOps%'
            THEN pt.DurationSeconds ELSE 0 END)                         AS security_scan_seconds,

        -- FIREWALL OVERHEAD
        SUM(CASE WHEN (
                pt.TaskName  LIKE '%firewall%'       OR pt.TaskName  LIKE '%Firewall%'
            OR  pt.TaskName  LIKE '%whitelist%'      OR pt.TaskName  LIKE '%Whitelist%'
            OR  pt.TaskName  LIKE 'Wait for firewall%'
            OR  pt.TaskName  LIKE '%Add firewall%'   OR pt.TaskName  LIKE '%Remove firewall%'
            OR  pt.TaskName  LIKE '%Identify resources%'
            OR  pt.JobName   LIKE '%Firewall%'       OR pt.JobName   LIKE '%firewall%'
            OR  pt.JobName   LIKE '%Whitelist%'
            OR  pt.StageName LIKE '%Firewall%'       OR pt.StageName LIKE '%firewall%'
            OR  pt.StageName LIKE '%Whitelist%'
            ) AND pt.TaskName NOT IN ('Initialize job', 'Finalize Job')
              AND pt.TaskName NOT LIKE 'Pre-job:%'
              AND pt.TaskName NOT LIKE 'Post-job:%'
            THEN pt.DurationSeconds ELSE 0 END)                         AS firewall_overhead_seconds,

        COUNT(DISTINCT CASE
            WHEN pt.TaskName NOT IN ('Initialize job', 'Finalize Job')
             AND pt.TaskName NOT LIKE 'Pre-job:%'
             AND pt.TaskName NOT LIKE 'Post-job:%'
             AND pt.TaskName NOT LIKE 'Microsoft Defender for DevOps%'
            THEN pt.TaskName
        END)                                                            AS unique_task_count,

        SUM(CASE WHEN dup.dup_count > 1 THEN 1 ELSE 0 END)             AS duplicate_task_occurrences

    FROM PipelineTasks pt
    LEFT JOIN (
        SELECT RunId, TaskName, COUNT(*) AS dup_count
        FROM   PipelineTasks
        WHERE  RunId = ?
        GROUP  BY RunId, TaskName
    ) dup ON pt.RunId = dup.RunId AND pt.TaskName = dup.TaskName
    WHERE pt.RunId = ?
    GROUP BY pt.RunId
),

test_features AS (
    SELECT
        RunId,
        CASE WHEN TotalTests  < 0 THEN 0 ELSE TotalTests  END          AS total_tests,
        CASE WHEN FailedTests  < 0 THEN 0 ELSE FailedTests  END        AS failed_test_count,
        CASE WHEN SkippedTests < 0 THEN 0 ELSE SkippedTests END        AS skipped_test_count,
        CASE WHEN TotalTests   > 0
             THEN PassedTests * 1.0 / NULLIF(TotalTests, 0)
             ELSE NULL
        END                                                             AS test_pass_rate
    FROM PipelineTests
    WHERE RunId = ?
)

SELECT
    -- Actual duration returned separately — NOT a model input
    pr.TotalDurationSeconds                                             AS total_duration_seconds,

    -- run context features
    pr.QueueWaitSeconds                                                 AS queue_wait_seconds,
    CASE WHEN pr.Result = 'failed'             THEN 1 ELSE 0 END        AS result_failed,
    CASE WHEN pr.Result = 'partiallySucceeded' THEN 1 ELSE 0 END        AS result_partial,
    CASE WHEN pr.Reason = 'schedule'           THEN 1 ELSE 0 END        AS is_scheduled,
    CASE WHEN pr.Branch LIKE '%main%'          THEN 1 ELSE 0 END        AS is_main_branch,

    -- job-level
    COALESCE(jf.job_count,                0)                            AS job_count,
    COALESCE(jf.failed_job_count,         0)                            AS failed_job_count,
    COALESCE(jf.max_job_duration_seconds, 0)                            AS max_job_duration_seconds,
    COALESCE(jf.avg_job_duration_seconds, 0)                            AS avg_job_duration_seconds,

    -- stage-level
    COALESCE(sf.stage_count,              0)                            AS stage_count,
    COALESCE(sf.failed_stage_count,       0)                            AS failed_stage_count,

    -- task-level
    COALESCE(tf.task_count,               0)                            AS task_count,
    COALESCE(tf.failed_task_count,        0)                            AS failed_task_count,
    COALESCE(tf.skipped_task_count,       0)                            AS skipped_task_count,
    COALESCE(tf.restore_task_seconds,     0)                            AS restore_task_seconds,
    COALESCE(tf.download_task_seconds,    0)                            AS download_task_seconds,
    COALESCE(tf.build_task_seconds,       0)                            AS build_task_seconds,
    COALESCE(tf.test_task_seconds,        0)                            AS test_task_seconds,
    COALESCE(tf.deploy_task_seconds,      0)                            AS deploy_task_seconds,
    COALESCE(tf.security_scan_seconds,    0)                            AS security_scan_seconds,
    COALESCE(tf.firewall_overhead_seconds,0)                            AS firewall_overhead_seconds,
    COALESCE(tf.unique_task_count,        0)                            AS unique_task_count,
    COALESCE(tf.duplicate_task_occurrences,0)                           AS duplicate_task_occurrences,

    -- test features (gated: only populated when test tasks or test results exist)
    CASE WHEN COALESCE(tf.test_task_seconds,0) > 0
          OR  COALESCE(tstf.total_tests, 0) > 0
         THEN COALESCE(tstf.total_tests, 0)        ELSE 0    END        AS total_tests,
    CASE WHEN COALESCE(tf.test_task_seconds,0) > 0
          OR  COALESCE(tstf.total_tests, 0) > 0
         THEN COALESCE(tstf.failed_test_count, 0)  ELSE 0    END        AS failed_test_count,
    CASE WHEN COALESCE(tf.test_task_seconds,0) > 0
          OR  COALESCE(tstf.total_tests, 0) > 0
         THEN COALESCE(tstf.skipped_test_count, 0) ELSE 0    END        AS skipped_test_count,
    CASE WHEN COALESCE(tf.test_task_seconds,0) > 0
          AND COALESCE(tstf.total_tests, 0)    > 0
         THEN COALESCE(tstf.test_pass_rate, 0)
         ELSE NULL
    END                                                                 AS test_pass_rate,

    -- timeline / parallelism (pre-computed by backfill.py)
    COALESCE(ptf.DependencyRestoreSeconds, 0)                           AS dependency_restore_seconds,
    COALESCE(ptf.DownloadSeconds,          0)                           AS download_seconds,
    COALESCE(ptf.CompileSeconds,           0)                           AS compile_seconds,
    COALESCE(ptf.ParallelJobCount,         1)                           AS parallel_job_count,
    COALESCE(ptf.TotalTimelineSeconds,     0)                           AS total_timeline_seconds,

    CASE WHEN pr.TotalDurationSeconds > 0
         THEN CAST(COALESCE(ptf.TotalTimelineSeconds, 0) AS FLOAT)
              / pr.TotalDurationSeconds
         ELSE 1.0
    END                                                                 AS parallelism_ratio,

    CASE WHEN COALESCE(ptf.ParallelJobCount, 1) > 1
         THEN 1 ELSE 0
    END                                                                 AS has_parallel_execution,

    CASE WHEN COALESCE(tf.test_task_seconds, 0) > 0
          OR  COALESCE(tstf.total_tests, 0) > 0
         THEN 1 ELSE 0
    END                                                                 AS has_test_results

FROM PipelineRuns pr
LEFT JOIN job_features             jf   ON pr.RunId = jf.RunId
LEFT JOIN stage_features           sf   ON pr.RunId = sf.RunId
LEFT JOIN task_features            tf   ON pr.RunId = tf.RunId
LEFT JOIN test_features            tstf ON pr.RunId = tstf.RunId
LEFT JOIN PipelineTimelineFeatures ptf  ON pr.RunId = ptf.RunId
WHERE pr.RunId = ?
"""

# Number of ? placeholders in _FEATURE_SQL
# job(1) + stage(1) + task_dup_inner(1) + task_outer(1) + test(1) + main WHERE(1) = 6
_SQL_PARAMS = 6


_PHASE_TASK_CONTEXT_SQL = """
WITH classified AS (
        SELECT
                pt.RunId,
                pt.TaskName,
        pt.JobName,
        pt.StageName,
                pt.DurationSeconds,
                CASE
                        WHEN pt.TaskName LIKE '%NuGet%'
                            OR pt.TaskName LIKE 'Install PnDModules'
                            OR pt.TaskName LIKE '%npm%'
                            OR pt.TaskName LIKE '%Restore%'
                            OR pt.TaskName LIKE 'Install node%'
                            OR pt.TaskName LIKE 'Install npm%'
                        THEN 'restore'

                        WHEN pt.TaskName LIKE '%Download%'
                            OR pt.TaskName LIKE '%Checkout%'
                        THEN 'download'

                        WHEN pt.TaskName LIKE '%Build%'
                            OR pt.TaskName LIKE '%Compile%'
                            OR pt.TaskName LIKE '%compile%'
                            OR pt.TaskName LIKE '%MSBuild%'
                            OR pt.TaskName LIKE '%dotnet build%'
                            OR pt.TaskName LIKE '%DotNet Build%'
                        THEN 'build'

                        WHEN pt.TaskName LIKE '%Test%'
                            OR pt.TaskName LIKE '%Tests%'
                            OR pt.TaskName LIKE '%Playwright%'
                            OR pt.TaskName LIKE '%Cypress%'
                            OR pt.TaskName LIKE '%VSTest%'
                            OR pt.TaskName LIKE '%Jest%'
                            OR pt.TaskName LIKE '%SilkTest%'
                        THEN 'test'

                        WHEN pt.TaskName LIKE '%Deploy%'
                            OR pt.TaskName LIKE '%Deploying%'
                            OR pt.TaskName LIKE '%Publish%'
                            OR pt.TaskName LIKE '%publish%'
                            OR pt.TaskName LIKE 'Deploy Helm%'
                            OR pt.TaskName LIKE 'AzureWebApp'
                        THEN 'deploy'

                        WHEN pt.TaskName LIKE '%Black%'
                            OR pt.TaskName LIKE '%Polaris%'
                            OR pt.TaskName LIKE '%BinSkim%'
                            OR pt.TaskName LIKE '%Malware%'
                            OR pt.TaskName LIKE '%Defender%'
                            OR pt.TaskName LIKE '%Coverity%'
                            OR pt.TaskName LIKE '%CredScan%'
                            OR pt.TaskName LIKE '%SDL%'
                            OR pt.TaskName LIKE '%Synopsys%'
                            OR pt.TaskName LIKE '%Whitesource%'
                            OR pt.TaskName LIKE '%AntiMalware%'
                            OR pt.JobName LIKE '%Security%'
                            OR pt.JobName LIKE '%Polaris%'
                            OR pt.JobName LIKE '%SDL%'
                            OR pt.JobName LIKE '%Compliance%'
                            OR pt.JobName LIKE '%Coverity%'
                            OR pt.JobName LIKE '%Scan%'
                            OR pt.JobName LIKE '%Synopsys%'
                            OR pt.JobName LIKE '%Black%'
                            OR pt.StageName LIKE '%Security%'
                            OR pt.StageName LIKE '%SDL%'
                            OR pt.StageName LIKE '%Compliance%'
                            OR pt.StageName LIKE '%Polaris%'
                            OR pt.StageName LIKE '%Scan%'
                            OR pt.StageName LIKE '%Coverity%'
                            OR pt.StageName LIKE '%Black%'
                        THEN 'security_scan'

                        WHEN pt.TaskName LIKE '%firewall%'
                            OR pt.TaskName LIKE '%Firewall%'
                            OR pt.TaskName LIKE '%whitelist%'
                            OR pt.TaskName LIKE '%Whitelist%'
                            OR pt.TaskName LIKE 'Wait for firewall%'
                            OR pt.TaskName LIKE '%Add firewall%'
                            OR pt.TaskName LIKE '%Remove firewall%'
                            OR pt.TaskName LIKE '%Identify resources%'
                            OR pt.JobName LIKE '%Firewall%'
                            OR pt.JobName LIKE '%firewall%'
                            OR pt.JobName LIKE '%Whitelist%'
                            OR pt.StageName LIKE '%Firewall%'
                            OR pt.StageName LIKE '%firewall%'
                            OR pt.StageName LIKE '%Whitelist%'
                        THEN 'firewall'
                        ELSE NULL
                END AS phase
        FROM PipelineTasks pt
        WHERE pt.RunId = ?
            AND pt.TaskName NOT IN ('Initialize job', 'Finalize Job')
            AND pt.TaskName NOT LIKE 'Pre-job:%'
            AND pt.TaskName NOT LIKE 'Post-job:%'
            AND pt.TaskName NOT LIKE 'Microsoft Defender for DevOps%'
),
ranked AS (
        SELECT
                phase,
                TaskName,
        JobName,
        StageName,
                DurationSeconds,
                ROW_NUMBER() OVER (
                        PARTITION BY phase
                        ORDER BY DurationSeconds DESC, TaskName ASC
                ) AS rn
        FROM classified
        WHERE phase IS NOT NULL
)
SELECT phase, TaskName, JobName, StageName, DurationSeconds
FROM ranked
WHERE rn <= ?
ORDER BY phase, rn;
"""


def build_features(run_id: int) -> tuple[pd.DataFrame, int]:
    """
    Extract features for a single run.

    Returns:
        features_df      — DataFrame with exactly MODEL_FEATURES columns in locked order.
        actual_duration  — TotalDurationSeconds (NOT passed to model; used for opportunity calc).

    Raises:
        ValueError if the run is not found or the feature schema is inconsistent.
    """
    params = (run_id,) * _SQL_PARAMS
    with get_conn() as conn:
        df = pd.read_sql(_FEATURE_SQL, conn, params=params)

    if df.empty:
        raise ValueError(
            f"No pipeline data found for RunId={run_id}. "
            "Run must exist in PipelineRuns with FinishTime set."
        )

    actual_duration = int(df["total_duration_seconds"].iloc[0] or 0)

    # Validate schema integrity
    missing = [c for c in MODEL_FEATURES if c not in df.columns]
    if missing:
        raise ValueError(
            f"Feature schema mismatch — missing columns: {missing}. "
            "Check that all Week 3 tables exist and feature_extraction.sql is up to date."
        )

    # test_pass_rate is NULL when no tests ran — fill with 1.0 (perfect pass)
    # to match the imputation used during training.
    df["test_pass_rate"] = df["test_pass_rate"].fillna(1.0).infer_objects(copy=False)

    return df[MODEL_FEATURES].copy(), actual_duration


def get_phase_task_context(run_id: int, top_n_per_phase: int = 3) -> dict[str, list[str]]:
    """Return top task/job/stage titles per phase for LLM grounding."""
    with get_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(_PHASE_TASK_CONTEXT_SQL, (run_id, top_n_per_phase))
        columns = [col[0] for col in cursor.description]
        context_df = pd.DataFrame.from_records(cursor.fetchall(), columns=columns)

    if context_df.empty:
        return {}

    phase_context: dict[str, list[str]] = {}
    for _, row in context_df.iterrows():
        phase = str(row["phase"])
        task_name = str(row["TaskName"]) if row["TaskName"] is not None else ""
        job_name = str(row["JobName"]) if row["JobName"] is not None else ""
        stage_name = str(row["StageName"]) if row["StageName"] is not None else ""
        duration_seconds = int(row["DurationSeconds"] or 0)
        duration_min = round(duration_seconds / 60.0, 1)

        # Build label: "Task: X (1.2 min) in Job: Y, Stage: Z"
        # Duration is always the task duration from PipelineTasks.
        parts = [f"Task: {task_name} ({duration_min} min)"]
        if job_name:
            parts.append(f"Job: {job_name}")
        if stage_name:
            parts.append(f"Stage: {stage_name}")

        phase_context.setdefault(phase, []).append(" | ".join(parts))

    return phase_context


_DUPLICATE_TASKS_SQL = """
SELECT TaskName, COUNT(*) AS occurrence_count
FROM PipelineTasks
WHERE RunId = ?
  AND TaskName NOT IN ('Initialize job', 'Finalize Job')
  AND TaskName NOT LIKE 'Pre-job:%'
  AND TaskName NOT LIKE 'Post-job:%'
  AND TaskName NOT LIKE 'Microsoft Defender for DevOps%'
GROUP BY TaskName
HAVING COUNT(*) > 1
ORDER BY occurrence_count DESC, TaskName ASC
OFFSET 0 ROWS FETCH NEXT 5 ROWS ONLY
"""

_SKIPPED_TASKS_SQL = """
SELECT TOP 5 TaskName, COUNT(*) AS skip_count
FROM PipelineTasks
WHERE RunId = ?
  AND Result = 'skipped'
  AND TaskName NOT IN ('Initialize job', 'Finalize Job')
  AND TaskName NOT LIKE 'Pre-job:%'
  AND TaskName NOT LIKE 'Post-job:%'
  AND TaskName NOT LIKE 'Microsoft Defender for DevOps%'
GROUP BY TaskName
ORDER BY skip_count DESC, TaskName ASC
"""


def get_cross_cutting_task_context(run_id: int) -> dict[str, list[str]]:
    """
    Return named task lists for cross-cutting signals so recommendations
    can cite specific task names rather than just counts.

    Returns a dict with optional keys:
      "duplicate_tasks": ["Checkout Aveva.Apps (4×)", "npm install (2×)", ...]
      "skipped_tasks":   ["Deploy to UAT (3×)", "Run E2E Tests (2×)", ...]
    """
    result: dict[str, list[str]] = {}
    with get_conn() as conn:
        cursor = conn.cursor()

        cursor.execute(_DUPLICATE_TASKS_SQL, (run_id,))
        rows = cursor.fetchall()
        if rows:
            result["duplicate_tasks"] = [
                f"{row[0]} ({row[1]}\u00d7)" for row in rows
            ]

        cursor.execute(_SKIPPED_TASKS_SQL, (run_id,))
        rows = cursor.fetchall()
        if rows:
            result["skipped_tasks"] = [
                f"{row[0]} ({row[1]}\u00d7)" for row in rows
            ]

    return result

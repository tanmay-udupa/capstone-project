WITH job_features AS (

    SELECT
        RunId,
        COUNT(*)                                                        AS job_count,
        SUM(CASE WHEN Result = 'failed' THEN 1 ELSE 0 END)             AS failed_job_count,
        MAX(DurationSeconds)                                            AS max_job_duration_seconds,
        AVG(DurationSeconds * 1.0)                                     AS avg_job_duration_seconds
    FROM PipelineJobs
    GROUP BY RunId
),

stage_features AS (
    SELECT
        RunId,
        COUNT(*)                                                        AS stage_count,
        SUM(CASE WHEN Result = 'failed' THEN 1 ELSE 0 END)             AS failed_stage_count
    FROM PipelineStages
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
        THEN pt.DurationSeconds ELSE 0 END)                            AS restore_task_seconds,

        -- DOWNLOAD / CHECKOUT
        SUM(CASE WHEN (
            pt.TaskName LIKE '%Download%'
        OR  pt.TaskName LIKE '%Checkout%'
        ) AND pt.TaskName NOT LIKE 'Pre-job:%'
          AND pt.TaskName NOT LIKE 'Post-job:%'
        THEN pt.DurationSeconds ELSE 0 END)                            AS download_task_seconds,

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
        THEN pt.DurationSeconds ELSE 0 END)                            AS build_task_seconds,

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
        THEN pt.DurationSeconds ELSE 0 END)                            AS test_task_seconds,

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
        THEN pt.DurationSeconds ELSE 0 END)                            AS deploy_task_seconds,

        -- SECURITY SCANS
        -- Checks TaskName, JobName, and StageName for broader coverage
        -- since security tools often run via generic script tasks
        SUM(CASE WHEN (
            -- Task name patterns
            pt.TaskName  LIKE '%Black%'         OR pt.TaskName  LIKE '%Polaris%'
        OR  pt.TaskName  LIKE '%BinSkim%'       OR pt.TaskName  LIKE '%Malware%'
        OR  pt.TaskName  LIKE '%Defender%'      OR pt.TaskName  LIKE '%Coverity%'
        OR  pt.TaskName  LIKE '%CredScan%'      OR pt.TaskName  LIKE '%SDL%'
        OR  pt.TaskName  LIKE '%Synopsys%'      OR pt.TaskName  LIKE '%Whitesource%'
        OR  pt.TaskName  LIKE '%AntiMalware%'
            -- Job name patterns
        OR  pt.JobName   LIKE '%Security%'      OR pt.JobName   LIKE '%Polaris%'
        OR  pt.JobName   LIKE '%SDL%'           OR pt.JobName   LIKE '%Compliance%'
        OR  pt.JobName   LIKE '%Coverity%'      OR pt.JobName   LIKE '%Scan%'
        OR  pt.JobName   LIKE '%Synopsys%'      OR pt.JobName   LIKE '%Black%' 
            -- Stage name patterns
        OR  pt.StageName LIKE '%Security%'      OR pt.StageName LIKE '%SDL%'
        OR  pt.StageName LIKE '%Compliance%'    OR pt.StageName LIKE '%Polaris%'
        OR  pt.StageName LIKE '%Scan%'          OR pt.StageName LIKE '%Coverity%'
        OR  pt.StageName LIKE '%Black%' 
        ) AND pt.TaskName NOT IN ('Initialize job', 'Finalize Job')
          AND pt.TaskName NOT LIKE 'Pre-job:%'
          AND pt.TaskName NOT LIKE 'Post-job:%'
          AND pt.TaskName NOT LIKE 'Microsoft Defender for DevOps%'
        THEN pt.DurationSeconds ELSE 0 END)                            AS security_scan_seconds,

        -- FIREWALL OVERHEAD
        -- Checks TaskName, JobName, and StageName for broader coverage
        SUM(CASE WHEN (
            -- Task name patterns
            pt.TaskName  LIKE '%firewall%'       OR pt.TaskName  LIKE '%Firewall%'
        OR  pt.TaskName  LIKE '%whitelist%'      OR pt.TaskName  LIKE '%Whitelist%'
        OR  pt.TaskName  LIKE 'Wait for firewall%'
        OR  pt.TaskName  LIKE '%Add firewall%'   OR pt.TaskName  LIKE '%Remove firewall%'
        OR  pt.TaskName  LIKE '%Identify resources%'
            -- Job name patterns
        OR  pt.JobName   LIKE '%Firewall%'       OR pt.JobName   LIKE '%firewall%'
        OR  pt.JobName   LIKE '%Whitelist%'
            -- Stage name patterns
        OR  pt.StageName LIKE '%Firewall%'       OR pt.StageName LIKE '%firewall%'
        OR  pt.StageName LIKE '%Whitelist%'
        ) AND pt.TaskName NOT IN ('Initialize job', 'Finalize Job')
          AND pt.TaskName NOT LIKE 'Pre-job:%'
          AND pt.TaskName NOT LIKE 'Post-job:%'
        THEN pt.DurationSeconds ELSE 0 END)                            AS firewall_overhead_seconds,

        COUNT(DISTINCT CASE
            WHEN pt.TaskName NOT IN ('Initialize job','Finalize Job')
             AND pt.TaskName NOT LIKE 'Pre-job:%'
             AND pt.TaskName NOT LIKE 'Post-job:%'
             AND pt.TaskName NOT LIKE 'Microsoft Defender for DevOps%'
            THEN pt.TaskName
        END)                                                           AS unique_task_count,

        SUM(CASE WHEN dup.dup_count > 1 THEN 1 ELSE 0 END)             AS duplicate_task_occurrences

    FROM PipelineTasks pt
    LEFT JOIN (
        SELECT RunId, TaskName, COUNT(*) AS dup_count
        FROM PipelineTasks
        GROUP BY RunId, TaskName
    ) dup
        ON pt.RunId = dup.RunId
       AND pt.TaskName = dup.TaskName

    GROUP BY pt.RunId
),

test_features AS (
    SELECT
        RunId,
        CASE
            WHEN TotalTests < 0 THEN 0
            ELSE TotalTests
        END                                                             AS total_tests,
        CASE
            WHEN FailedTests < 0 THEN 0
            ELSE FailedTests
        END                                                             AS failed_test_count,
        CASE
            WHEN SkippedTests < 0 THEN 0
            ELSE SkippedTests
        END                                                             AS skipped_test_count,
        CASE
            WHEN TotalTests > 0
            THEN PassedTests * 1.0 / NULLIF(TotalTests, 0)
            ELSE NULL
        END                                                             AS test_pass_rate
    FROM PipelineTests
)


SELECT

    -- identifiers
    pr.RunId,
    pr.ProjectName,
    pr.PipelineName,

    -- target variable
    pr.TotalDurationSeconds                                             AS total_duration_seconds,

    -- run context features
    pr.QueueWaitSeconds                                                 AS queue_wait_seconds,
    CASE WHEN pr.Result    = 'failed'            THEN 1 ELSE 0 END     AS result_failed,
    CASE WHEN pr.Result    = 'partiallySucceeded' THEN 1 ELSE 0 END    AS result_partial,
    CASE WHEN pr.Reason    = 'schedule'          THEN 1 ELSE 0 END     AS is_scheduled,
    CASE WHEN pr.Branch LIKE '%main%'            THEN 1 ELSE 0 END     AS is_main_branch,

    -- job-level features
    COALESCE(jf.job_count,                0)                           AS job_count,
    COALESCE(jf.failed_job_count,         0)                           AS failed_job_count,
    COALESCE(jf.max_job_duration_seconds, 0)                           AS max_job_duration_seconds,
    COALESCE(jf.avg_job_duration_seconds, 0)                           AS avg_job_duration_seconds,

    -- stage-level features
    COALESCE(sf.stage_count,              0)                           AS stage_count,
    COALESCE(sf.failed_stage_count,       0)                           AS failed_stage_count,

    -- task-level features
    COALESCE(tf.task_count,               0)                           AS task_count,
    COALESCE(tf.failed_task_count,        0)                           AS failed_task_count,
    COALESCE(tf.skipped_task_count,       0)                           AS skipped_task_count,
    COALESCE(tf.restore_task_seconds,     0)                           AS restore_task_seconds,
    COALESCE(tf.download_task_seconds,    0)                           AS download_task_seconds,  -- FIX: was missing
    COALESCE(tf.build_task_seconds,       0)                           AS build_task_seconds,
    COALESCE(tf.test_task_seconds,        0)                           AS test_task_seconds,
    COALESCE(tf.deploy_task_seconds,      0)                           AS deploy_task_seconds,
    COALESCE(tf.security_scan_seconds,    0)                           AS security_scan_seconds,  -- FIX: was missing
    COALESCE(tf.firewall_overhead_seconds,0)                           AS firewall_overhead_seconds, -- FIX: was missing
    COALESCE(tf.unique_task_count,        0)                           AS unique_task_count,
    COALESCE(tf.duplicate_task_occurrences, 0)                         AS duplicate_task_occurrences,

    -- test features
    CASE
        WHEN COALESCE(tf.test_task_seconds, 0) > 0 OR COALESCE(tstf.total_tests, 0) > 0
        THEN COALESCE(tstf.total_tests, 0)
        ELSE 0
    END                                                                AS total_tests,
    CASE
        WHEN COALESCE(tf.test_task_seconds, 0) > 0 OR COALESCE(tstf.total_tests, 0) > 0
        THEN COALESCE(tstf.failed_test_count, 0)
        ELSE 0
    END                                                                AS failed_test_count,
    CASE
        WHEN COALESCE(tf.test_task_seconds, 0) > 0 OR COALESCE(tstf.total_tests, 0) > 0
        THEN COALESCE(tstf.skipped_test_count, 0)
        ELSE 0
    END                                                                AS skipped_test_count,
    CASE
        WHEN COALESCE(tf.test_task_seconds, 0) > 0 AND COALESCE(tstf.total_tests, 0) > 0
        THEN COALESCE(tstf.test_pass_rate, 0)
        WHEN COALESCE(tf.test_task_seconds, 0) = 0 AND COALESCE(tstf.total_tests, 0) = 0
        THEN NULL
        ELSE NULL
    END                                                                AS test_pass_rate,

    -- timeline/parallelism features (pre-computed in backfill)
    COALESCE(ptf.DependencyRestoreSeconds, 0)                          AS dependency_restore_seconds,
    COALESCE(ptf.DownloadSeconds,          0)                          AS download_seconds,
    COALESCE(ptf.CompileSeconds,           0)                          AS compile_seconds,
    COALESCE(ptf.ParallelJobCount,         1)                          AS parallel_job_count,
    COALESCE(ptf.TotalTimelineSeconds,     0)                          AS total_timeline_seconds,

    -- parallelism ratio: >1.0 = parallel (efficient), ~1.0 = sequential, <1.0 = idle gaps
    CASE
        WHEN pr.TotalDurationSeconds > 0
        THEN CAST(COALESCE(ptf.TotalTimelineSeconds, 0) AS FLOAT) / pr.TotalDurationSeconds
        ELSE 1.0
    END                                                                AS parallelism_ratio,

    CASE
        WHEN COALESCE(ptf.ParallelJobCount, 1) > 1
        THEN 1 ELSE 0
    END                                                                AS has_parallel_execution


FROM PipelineRuns pr

LEFT JOIN job_features              jf   ON pr.RunId = jf.RunId
LEFT JOIN stage_features            sf   ON pr.RunId = sf.RunId
LEFT JOIN task_features             tf   ON pr.RunId = tf.RunId
LEFT JOIN test_features             tstf ON pr.RunId = tstf.RunId
LEFT JOIN PipelineTimelineFeatures  ptf  ON pr.RunId = ptf.RunId

-- only include completed runs (skip queued/running)
WHERE pr.FinishTime IS NOT NULL

ORDER BY pr.RunId;

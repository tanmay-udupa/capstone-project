-- Migration 002: enforce one analysis row per RunId
-- Keeps the most recent AnalysisId for each RunId, removes older duplicates,
-- then adds a unique index on RunId.

WITH ranked AS (
    SELECT
        AnalysisId,
        RunId,
        ROW_NUMBER() OVER (
            PARTITION BY RunId
            ORDER BY AnalysisId DESC
        ) AS rn
    FROM dbo.AnalysisResults
)
DELETE FROM dbo.AnalysisResults
WHERE AnalysisId IN (
    SELECT AnalysisId FROM ranked WHERE rn > 1
);

IF NOT EXISTS (
    SELECT 1
    FROM sys.indexes
    WHERE object_id = OBJECT_ID('dbo.AnalysisResults')
      AND name = 'UX_AnalysisResults_RunId'
)
BEGIN
    CREATE UNIQUE NONCLUSTERED INDEX UX_AnalysisResults_RunId
        ON dbo.AnalysisResults (RunId)
        INCLUDE (Status, CompletedAt, StartedAt);
END;

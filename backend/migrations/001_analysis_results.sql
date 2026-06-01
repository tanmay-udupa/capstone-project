-- Migration 001: AnalysisResults table
-- Run this once against pipelinedb before starting the API.
--
-- RequestPayload stores the original AnalyzeRequest JSON so results
-- are reproducible and auditable.

CREATE TABLE dbo.AnalysisResults (
    AnalysisId      INT IDENTITY(1,1)    NOT NULL,
    OrgName         NVARCHAR(255)        NOT NULL,
    ProjectName     NVARCHAR(255)        NOT NULL,
    PipelineId      INT                  NOT NULL,
    RunId           INT                  NOT NULL,
    Status          NVARCHAR(20)         NOT NULL   -- pending | processing | complete | failed
                    CONSTRAINT CK_AnalysisResults_Status
                    CHECK (Status IN ('pending', 'processing', 'complete', 'failed')),
    ResultJson      NVARCHAR(MAX)        NULL,      -- JSON blob of AnalysisResult
    ErrorMessage    NVARCHAR(2000)       NULL,
    StartedAt       DATETIME2            NOT NULL   DEFAULT SYSUTCDATETIME(),
    CompletedAt     DATETIME2            NULL,
    RequestedBy     NVARCHAR(255)        NULL,
    RequestPayload  NVARCHAR(MAX)        NULL,      -- JSON blob of AnalyzeRequest
    CONSTRAINT PK_AnalysisResults PRIMARY KEY CLUSTERED (AnalysisId ASC)
);

-- Speed up polling by run
CREATE NONCLUSTERED INDEX IX_AnalysisResults_RunId
    ON dbo.AnalysisResults (RunId)
    INCLUDE (Status, CompletedAt);

-- Speed up listing by org/project
CREATE NONCLUSTERED INDEX IX_AnalysisResults_OrgProject
    ON dbo.AnalysisResults (OrgName, ProjectName, Status);

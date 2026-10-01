from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────────────

class BenchmarkPolicy(str, Enum):
    MEDIAN_SAME_STAGES = "median_same_stages"


class ComparisonScope(str, Enum):
    PIPELINE_FAMILY = "pipeline_family"
    PROJECT         = "project"
    ORGANIZATION    = "organization"


class DataSufficiency(str, Enum):
    HIGH   = "high"
    MEDIUM = "medium"
    LOW    = "low"


class FallbackUsed(str, Enum):
    NONE               = "none"
    BENCHMARK_DISABLED = "benchmark_disabled"


class AnalysisStatus(str, Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    COMPLETE = "complete"
    FAILED   = "failed"


# ── Request ────────────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    org:                     str = Field(..., min_length=1, max_length=100)
    project:                 str = Field(..., min_length=1, max_length=100)
    pipeline_id:             int = Field(..., gt=0)
    run_id:                  int = Field(..., gt=0)
    top_k_recommendations:   int = Field(default=3,  ge=1, le=10)
    min_opportunity_seconds: int = Field(default=60, ge=0)


# ── Nested response objects ────────────────────────────────────────────────────

class RunMetrics(BaseModel):
    actual_duration_seconds:    int
    predicted_expected_seconds: int
    expected_gap_seconds:       int
    expected_gap_pct:           float


class BenchmarkResult(BaseModel):
    policy_used:            BenchmarkPolicy
    scope_used:             ComparisonScope
    sample_size:            int
    data_sufficiency:       DataSufficiency
    fallback_used:          FallbackUsed
    observed_agent_seconds: int   # task durations summed across every job in this run
    typical_agent_seconds:  int   # median of the baseline runs


class TopContributor(BaseModel):
    feature:               str
    label:                 str
    feature_value_seconds: int
    shap_impact_seconds:   int


class DiagnosisResult(BaseModel):
    top_contributors: list[TopContributor]


class PhaseComparison(BaseModel):
    phase:                 str
    observed_seconds:      int
    typical_seconds:       int
    p90_seconds:           int
    above_typical_seconds: int


class Recommendation(BaseModel):
    id:                      str
    title:                   str
    description:             str   # LLM-generated or template fallback
    reason_codes:            list[str]
    figure_seconds:          int | None    # measured time behind the finding
    figure_label:            str | None
    share_of_agent_time_pct: float | None
    observed_seconds:        int | None = None
    typical_seconds:         int | None = None


class DecisionSummary(BaseModel):
    message: str


class Versions(BaseModel):
    api_version:                  str = "1.0.0"
    model_version:                str = "unknown"
    feature_schema_version:       str = "1"
    benchmark_policy_version:     str = "1"
    recommendation_rules_version: str = "1"


# ── Top-level response schemas ─────────────────────────────────────────────────

class AnalyzeResponse(BaseModel):
    analysis_id: int
    status:      AnalysisStatus
    message:     str
    poll_url:    str


class AnalysisInput(BaseModel):
    org:         str
    project:     str
    pipeline_id: int
    run_id:      int


class AnalysisResult(BaseModel):
    analysis_id:          int
    status:               AnalysisStatus
    requested_at_utc:     str | None             = None
    completed_at_utc:     str | None             = None
    error_message:        str | None             = None
    input:                AnalysisInput | None   = None
    run_metrics:          RunMetrics | None      = None
    benchmark:            BenchmarkResult | None = None
    diagnosis:            DiagnosisResult | None = None
    phase_comparison:     list[PhaseComparison] | None = None
    recommendations:      list[Recommendation] | None  = None
    decision_summary:     DecisionSummary | None       = None
    versions:             Versions | None              = None


class RecommendationsOnly(BaseModel):
    analysis_id:      int
    status:           AnalysisStatus
    recommendations:  list[Recommendation]
    decision_summary: DecisionSummary | None = None


class HealthResponse(BaseModel):
    status:       str
    model_loaded: bool
    db_reachable: bool


class ErrorDetail(BaseModel):
    code:    str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


# ── ADO browsing schemas (org → project → pipeline → run) ─────────────────────

class AdoOrganization(BaseModel):
    id:   str
    name: str
    url:  str


class AdoProject(BaseModel):
    id:          str
    name:        str
    state:       str
    description: str = ""


class AdoPipeline(BaseModel):
    id:     int
    name:   str
    folder: str = "\\"


class AdoRun(BaseModel):
    id:               int
    name:             str
    state:            str          # Build API status: completed | inProgress | notStarted | cancelling | postponed
    result:           str | None   # succeeded | failed | canceled | partiallySucceeded
    created_date:     str | None
    finished_date:    str | None
    duration_seconds: int | None   # None when run is still in progress
    branch:           str | None = None   # e.g. refs/heads/main


class AdoOrganizationsResponse(BaseModel):
    organizations: list[AdoOrganization]


class AdoProjectsResponse(BaseModel):
    org:      str
    projects: list[AdoProject]


class AdoPipelinesResponse(BaseModel):
    org:       str
    project:   str
    pipelines: list[AdoPipeline]


class AdoRunsResponse(BaseModel):
    org:         str
    project:     str
    pipeline_id: int
    runs:        list[AdoRun]


# ── Insights (Pipeline Efficiency page) ────────────────────────────────────────

class InsightsScope(BaseModel):
    project:                   str | None
    days:                      int | None
    runs:                      int
    pipelines:                 int
    first_run_utc:             str | None
    last_run_utc:              str | None
    span_days:                 int
    agent_seconds:             int
    incomplete_before_utc:     str | None   # stored runs before this have gaps (insights.MIN_GAP_DAYS)
    agent_seconds_per_30_days: int | None   # from runs since incomplete_before_utc; None if under 7 days


class ComputeBreakdown(BaseModel):
    name:          str
    runs:          int
    agent_seconds: int


class RelatedRun(BaseModel):
    run_id: int
    result: str
    url:    str | None


class ExampleRun(BaseModel):
    run_id:        int
    pipeline:      str
    started_utc:   str | None
    agent_seconds: int
    url:           str | None
    related:       RelatedRun | None   # the other run of the same commit, for categories about pairs


class FailedTaskCount(BaseModel):
    pipeline: str
    task:     str
    runs:     int


class WasteCategory(BaseModel):
    key:              str
    title:            str
    runs:             int
    agent_seconds:    int
    likely_avoidable: bool
    fix:              str
    pipelines:        list[ComputeBreakdown]
    examples:         list[ExampleRun]        # most agent time first
    related_label:    str | None              # how each example relates to its related run, e.g. "Passed in"
    failed_tasks:     list[FailedTaskCount]   # how many runs in this category the task failed in


class PipelineCompute(BaseModel):
    pipeline:                   str
    runs:                       int
    agent_seconds:              int
    failed_or_canceled_seconds: int


class TaskCompute(BaseModel):
    pipeline:      str
    task:          str
    runs:          int
    occurrences:   int
    agent_seconds: int


class RepeatedTask(BaseModel):
    pipeline:          str
    task:              str
    runs:              int
    extra_occurrences: int
    repeated_seconds:  int


class ProjectOption(BaseModel):
    name: str
    runs: int


class InsightsProjectsResponse(BaseModel):
    projects: list[ProjectOption]


class InsightsResponse(BaseModel):
    scope:          InsightsScope
    by_outcome:     list[ComputeBreakdown]
    by_pool:        list[ComputeBreakdown]
    waste:          list[WasteCategory]
    top_pipelines:  list[PipelineCompute]
    top_tasks:      list[TaskCompute]
    repeated_tasks: list[RepeatedTask]

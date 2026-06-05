from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Enums ──────────────────────────────────────────────────────────────────────

class BenchmarkPolicy(str, Enum):
    FRONTIER_P10   = "frontier_p10"
    FRONTIER_P20   = "frontier_p20"
    PERCENTILE_P25 = "percentile_p25"
    SLO_ONLY       = "slo_only"
    HYBRID         = "hybrid_frontier_slo"


class ComparisonScope(str, Enum):
    PIPELINE_FAMILY = "pipeline_family"
    PROJECT         = "project"
    ORGANIZATION    = "organization"


class DataSufficiency(str, Enum):
    HIGH   = "high"
    MEDIUM = "medium"
    LOW    = "low"


class FallbackUsed(str, Enum):
    NONE                = "none"
    PIPELINE_TO_PROJECT = "pipeline_to_project"
    PROJECT_TO_ORG      = "project_to_org"
    BENCHMARK_DISABLED  = "benchmark_disabled"


class AnalysisStatus(str, Enum):
    PENDING  = "pending"
    RUNNING  = "running"
    COMPLETE = "complete"
    FAILED   = "failed"


class Actionability(str, Enum):
    HIGH   = "high"
    MEDIUM = "medium"
    LOW    = "low"


class Priority(str, Enum):
    HIGH   = "high"
    MEDIUM = "medium"
    LOW    = "low"


# ── Request ────────────────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    org:                     str            = Field(..., min_length=1, max_length=100)
    project:                 str            = Field(..., min_length=1, max_length=100)
    pipeline_id:             int            = Field(..., gt=0)
    run_id:                  int            = Field(..., gt=0)
    benchmark_policy:        BenchmarkPolicy  = BenchmarkPolicy.FRONTIER_P20
    comparison_scope:        ComparisonScope  = ComparisonScope.PIPELINE_FAMILY
    top_k_recommendations:   int            = Field(default=3,    ge=1, le=10)
    min_confidence:          float          = Field(default=0.10, ge=0.0, le=1.0)
    min_opportunity_seconds: int            = Field(default=60,   ge=0)
    min_shap_impact_seconds: int            = Field(default=1,    ge=0)


# ── Nested response objects ────────────────────────────────────────────────────

class RunMetrics(BaseModel):
    actual_duration_seconds:    int
    predicted_expected_seconds: int
    expected_gap_seconds:       int
    expected_gap_pct:           float


class BenchmarkResult(BaseModel):
    policy_used:               BenchmarkPolicy
    scope_used:                ComparisonScope
    target_total_seconds:      int
    total_opportunity_seconds: int
    total_opportunity_pct:     float
    sample_size:               int
    data_sufficiency:          DataSufficiency
    fallback_used:             FallbackUsed


class TopContributor(BaseModel):
    feature:               str
    label:                 str
    feature_value_seconds: int
    shap_impact_seconds:   int


class DiagnosisResult(BaseModel):
    top_contributors: list[TopContributor]


class OpportunityByPhase(BaseModel):
    phase:               str
    observed_seconds:    int
    benchmark_seconds:   int
    opportunity_seconds: int


class Recommendation(BaseModel):
    id:                        str
    title:                     str
    description:               str   # LLM-generated or template fallback
    reason_codes:              list[str]
    priority:                  Priority
    confidence:                float
    estimated_savings_seconds: int
    estimated_savings_pct:     float  # Backward-compatible alias of pct_of_run
    estimated_savings_pct_of_run: float
    estimated_savings_pct_of_phase: float


class DecisionSummary(BaseModel):
    actionability: Actionability
    message:       str


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
    opportunity_by_phase: list[OpportunityByPhase] | None = None
    recommendations:      list[Recommendation] | None     = None
    decision_summary:     DecisionSummary | None          = None
    versions:             Versions | None                 = None


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
    state:            str          # inProgress | completed | canceling | unknown
    result:           str | None   # succeeded | failed | canceled | partiallySucceeded
    created_date:     str | None
    finished_date:    str | None
    duration_seconds: int | None   # None when run is still in progress


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

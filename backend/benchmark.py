from __future__ import annotations

import logging
from dataclasses import dataclass

from config import settings
from database import get_conn
from feature_builder import PHASE_FEATURES
from schemas import BenchmarkPolicy, ComparisonScope, DataSufficiency, FallbackUsed

logger = logging.getLogger(__name__)


@dataclass
class PhaseBenchmark:
    phase:               str
    observed_seconds:    int
    benchmark_seconds:   int
    opportunity_seconds: int


@dataclass
class BenchmarkOutput:
    policy_used:               BenchmarkPolicy
    scope_used:                ComparisonScope
    target_total_seconds:      int
    total_opportunity_seconds: int
    total_opportunity_pct:     float
    sample_size:               int
    data_sufficiency:          DataSufficiency
    fallback_used:             FallbackUsed
    phases:                    list[PhaseBenchmark]


# ── Internal helpers ───────────────────────────────────────────────────────────

def _quantile(policy: BenchmarkPolicy) -> float:
    return {
        BenchmarkPolicy.FRONTIER_P10:   0.10,
        BenchmarkPolicy.FRONTIER_P20:   0.20,
        BenchmarkPolicy.PERCENTILE_P25: 0.25,
        BenchmarkPolicy.SLO_ONLY:       0.20,
        BenchmarkPolicy.HYBRID:         0.20,
    }.get(policy, 0.20)


def _sufficiency(n: int) -> DataSufficiency:
    if n >= settings.BENCHMARK_MIN_SAMPLES_HIGH:
        return DataSufficiency.HIGH
    if n >= settings.BENCHMARK_MIN_SAMPLES_MEDIUM:
        return DataSufficiency.MEDIUM
    return DataSufficiency.LOW


def _query_benchmark(
    pipeline_name: str | None,
    project:       str | None,
    scope:         ComparisonScope,
    quantile:      float,
) -> tuple[int, dict[str, int], int]:
    """
    Query PipelineFeaturesMaterialized for benchmark percentiles at the given scope.

    Returns:
        (sample_size, {phase_col: target_seconds}, target_total_seconds)
        All zeros when no data is available.
    """
    phase_cols = list(PHASE_FEATURES.values())

    # Build scope filter
    if scope == ComparisonScope.PIPELINE_FAMILY and pipeline_name:
        where  = "WHERE PipelineName = ?"
        params: tuple = (pipeline_name,)
    elif scope == ComparisonScope.PROJECT and project:
        where  = "WHERE ProjectName = ?"
        params = (project,)
    else:
        where  = ""
        params = ()

    # Build PERCENTILE_CONT selects for each phase column + total duration
    phase_selects = "\n        ".join(
        f"PERCENTILE_CONT({quantile}) WITHIN GROUP (ORDER BY {col})"
        f" OVER () AS bm_{col},"
        for col in phase_cols
    )

    sql = f"""
    SELECT TOP 1
        COUNT(*) OVER ()                                                AS sample_size,
        {phase_selects}
        PERCENTILE_CONT({quantile}) WITHIN GROUP (ORDER BY total_duration_seconds)
            OVER ()                                                     AS bm_total_duration
    FROM PipelineFeaturesMaterialized
    {where}
    """

    with get_conn() as conn:
        try:
            row = conn.execute(sql, params).fetchone()
        except Exception as exc:
            # Table / view not yet created — cold-start zero
            logger.warning(
                "Benchmark query failed for scope=%s pipeline=%s project=%s: %s",
                scope,
                pipeline_name,
                project,
                exc,
            )
            return 0, {col: 0 for col in phase_cols}, 0

    if not row or int(row[0]) == 0:
        return 0, {col: 0 for col in phase_cols}, 0

    sample_size  = int(row[0])
    target_total = int(row.bm_total_duration or 0)
    phase_targets = {
        col: int(getattr(row, f"bm_{col}") or 0)
        for col in phase_cols
    }
    return sample_size, phase_targets, target_total


# ── Public API ─────────────────────────────────────────────────────────────────

def compute_benchmark(
    run_id:            int,
    pipeline_name:     str | None,
    project:           str | None,
    org:               str | None,
    scope:             ComparisonScope,
    policy:            BenchmarkPolicy,
    observed_features: dict[str, int],   # {feature_col: observed_seconds}
    actual_duration:   int,
) -> BenchmarkOutput:
    """
    Compute benchmark targets and per-phase opportunity gaps.

    Fallback chain (stops at first scope with sufficient data):
      pipeline_family → project → organization → benchmark_disabled
    """
    quantile      = _quantile(policy)
    current_scope = scope
    fallback_used = FallbackUsed.NONE

    fallback_chain = [
        (scope,                         FallbackUsed.NONE),
        (ComparisonScope.PROJECT,        FallbackUsed.PIPELINE_TO_PROJECT),
        (ComparisonScope.ORGANIZATION,   FallbackUsed.PROJECT_TO_ORG),
    ]

    sample_size   = 0
    phase_targets = {col: 0 for col in PHASE_FEATURES.values()}
    target_total  = 0

    for attempt_scope, fb_label in fallback_chain:
        sample_size, phase_targets, target_total = _query_benchmark(
            pipeline_name, project, attempt_scope, quantile
        )
        current_scope = attempt_scope
        fallback_used = fb_label

        # Stop if we have medium or better data quality
        if _sufficiency(sample_size) != DataSufficiency.LOW:
            break

    # Completely unavailable
    if sample_size == 0 or target_total == 0:
        return BenchmarkOutput(
            policy_used=policy,
            scope_used=current_scope,
            target_total_seconds=0,
            total_opportunity_seconds=0,
            total_opportunity_pct=0.0,
            sample_size=0,
            data_sufficiency=DataSufficiency.LOW,
            fallback_used=FallbackUsed.BENCHMARK_DISABLED,
            phases=[],
        )

    # Per-phase opportunity
    phases: list[PhaseBenchmark] = []
    for phase_label, col in PHASE_FEATURES.items():
        observed = observed_features.get(col, 0)
        target   = phase_targets.get(col, 0)
        if target == 0:
            continue
        opportunity = max(0, observed - target)
        phases.append(PhaseBenchmark(
            phase=phase_label,
            observed_seconds=observed,
            benchmark_seconds=target,
            opportunity_seconds=opportunity,
        ))

    total_opp = max(0, actual_duration - target_total)
    opp_pct   = (
        round(total_opp / actual_duration * 100, 1)
        if actual_duration > 0 else 0.0
    )

    return BenchmarkOutput(
        policy_used=policy,
        scope_used=current_scope,
        target_total_seconds=target_total,
        total_opportunity_seconds=total_opp,
        total_opportunity_pct=opp_pct,
        sample_size=sample_size,
        data_sufficiency=_sufficiency(sample_size),
        fallback_used=fallback_used,
        phases=phases,
    )

from __future__ import annotations

import logging
from dataclasses import dataclass

from config import settings
from database import get_conn
from feature_builder import PHASE_FEATURES, build_features_for_runs
from schemas import BenchmarkPolicy, ComparisonScope, DataSufficiency, FallbackUsed

logger = logging.getLogger(__name__)

BENCHMARK_POLICY_VERSION = "3"
_BASELINE_RUNS = 30
_MIN_BASELINE_RUNS = 5
_CANDIDATE_RUNS = 500

# Queue wait happens before an agent is assigned, so it isn't agent time.
AGENT_TIME_PHASES: dict[str, str] = {
    phase: column for phase, column in PHASE_FEATURES.items() if phase != "queue"
}

_RUN_STAGES_SQL = """
SELECT StageName
FROM PipelineStages
WHERE RunId = ?
  AND Result <> 'skipped'
"""

# One row per (candidate run, stage it ran); StageName is NULL when the run has no stages recorded.
_CANDIDATE_STAGES_SQL = """
WITH candidates AS (
    SELECT TOP (?) RunId
    FROM PipelineRuns
    WHERE PipelineName = ?
      AND RunId < ?
      AND Result IN ('succeeded', 'partiallySucceeded')
      AND FinishTime IS NOT NULL
    ORDER BY RunId DESC
)
SELECT c.RunId, s.StageName
FROM candidates c
LEFT JOIN PipelineStages s
       ON s.RunId = c.RunId
      AND s.Result <> 'skipped'
"""


@dataclass
class PhaseBenchmark:
    phase:                 str
    observed_seconds:      int
    typical_seconds:       int   # median of the baseline runs
    p90_seconds:           int   # 9 in 10 baseline runs used no more than this
    above_typical_seconds: int

    @property
    def is_unusual(self) -> bool:
        return self.observed_seconds > self.p90_seconds


@dataclass
class BenchmarkOutput:
    policy_used:            BenchmarkPolicy
    scope_used:             ComparisonScope
    sample_size:            int
    data_sufficiency:       DataSufficiency
    fallback_used:          FallbackUsed
    observed_agent_seconds: int
    typical_agent_seconds:  int
    phases:                 list[PhaseBenchmark]


# ── Internal helpers ───────────────────────────────────────────────────────────

def _sufficiency(n: int) -> DataSufficiency:
    if n >= settings.BENCHMARK_MIN_SAMPLES_HIGH:
        return DataSufficiency.HIGH
    if n >= settings.BENCHMARK_MIN_SAMPLES_MEDIUM:
        return DataSufficiency.MEDIUM
    return DataSufficiency.LOW


def _comparable_run_ids(run_id: int, pipeline_name: str) -> list[int]:
    """Most recent earlier completed runs of the pipeline that ran exactly the same stages as this run."""
    with get_conn() as conn:
        own_stages = {row[0] for row in conn.execute(_RUN_STAGES_SQL, run_id).fetchall()}
        rows = conn.execute(_CANDIDATE_STAGES_SQL, _CANDIDATE_RUNS, pipeline_name, run_id).fetchall()

    stages_by_run: dict[int, set[str]] = {}
    for candidate_id, stage_name in rows:
        stages = stages_by_run.setdefault(int(candidate_id), set())
        if stage_name is not None:
            stages.add(stage_name)

    comparable = [rid for rid in sorted(stages_by_run, reverse=True) if stages_by_run[rid] == own_stages]
    return comparable[:_BASELINE_RUNS]


# ── Public API ─────────────────────────────────────────────────────────────────

def compute_benchmark(
    run_id:                 int,
    pipeline_name:          str | None,
    observed_features:      dict[str, int],   # {feature_col: observed_seconds}
    observed_agent_seconds: int,
) -> BenchmarkOutput:
    """
    Compare this run's agent time per phase with up to 30 earlier runs of the same pipeline that
    ran exactly the same stages and succeeded or partially succeeded.

    Matching stages keeps runs that skipped a heavy stage (such as security scans) out of the baseline.
    Both sides come from the same feature query, so phases are defined identically.
    With fewer than 5 comparable runs the comparison is disabled rather than borrowed from other runs.
    """
    baseline_ids = _comparable_run_ids(run_id, pipeline_name) if pipeline_name else []
    baseline = build_features_for_runs(baseline_ids) if baseline_ids else None
    if baseline is not None:
        # A completed run with no agent time means its jobs were never stored.
        baseline = baseline[baseline["total_timeline_seconds"] > 0]
    sample_size = 0 if baseline is None else len(baseline)

    if baseline is None or sample_size < _MIN_BASELINE_RUNS:
        logger.info("Benchmark disabled for run %d: %d comparable runs", run_id, sample_size)
        return BenchmarkOutput(
            policy_used=BenchmarkPolicy.MEDIAN_SAME_STAGES,
            scope_used=ComparisonScope.PIPELINE_FAMILY,
            sample_size=sample_size,
            data_sufficiency=DataSufficiency.LOW,
            fallback_used=FallbackUsed.BENCHMARK_DISABLED,
            observed_agent_seconds=observed_agent_seconds,
            typical_agent_seconds=0,
            phases=[],
        )

    phases: list[PhaseBenchmark] = []
    for phase, column in AGENT_TIME_PHASES.items():
        values   = baseline[column].astype(float)
        observed = int(observed_features.get(column, 0))
        typical  = int(round(values.median()))
        p90      = int(round(values.quantile(0.9)))
        if observed == typical == p90 == 0:
            continue
        phases.append(PhaseBenchmark(
            phase=phase,
            observed_seconds=observed,
            typical_seconds=typical,
            p90_seconds=p90,
            above_typical_seconds=max(0, observed - typical),
        ))

    return BenchmarkOutput(
        policy_used=BenchmarkPolicy.MEDIAN_SAME_STAGES,
        scope_used=ComparisonScope.PIPELINE_FAMILY,
        sample_size=sample_size,
        data_sufficiency=_sufficiency(sample_size),
        fallback_used=FallbackUsed.NONE,
        observed_agent_seconds=observed_agent_seconds,
        typical_agent_seconds=int(round(baseline["total_timeline_seconds"].astype(float).median())),
        phases=phases,
    )

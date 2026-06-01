from __future__ import annotations

import logging

from config import settings
from benchmark import BenchmarkOutput
from feature_builder import PHASE_FEATURES
from schemas import Actionability, DataSufficiency, Priority

logger = logging.getLogger(__name__)

# ── Phase recommendation templates ────────────────────────────────────────────
# (title, action_hint) — used both for the template fallback and LLM prompt context
PHASE_TEMPLATES: dict[str, tuple[str, str]] = {
    "queue": (
        "Reduce queue wait time",
        "Increase agent pool capacity, use self-hosted agents, or stagger pipeline triggers "
        "to reduce concurrent demand. Check if the queue peak coincides with scheduled builds.",
    ),
    "restore": (
        "Speed up package restore",
        "Enable pipeline caching for NuGet/npm packages keyed on the lockfile hash. "
        "Consider switching to a private package feed with a closer network location.",
    ),
    "download": (
        "Reduce artifact download time",
        "Cache large build artefacts between pipeline stages. "
        "Evaluate whether all artefacts are required in every run.",
    ),
    "build": (
        "Accelerate build / compile step",
        "Enable incremental builds (only rebuild changed targets). "
        "Consider distributing compilation across agents or using a build cache.",
    ),
    "test": (
        "Optimise test execution time",
        "Parallelize test runs across multiple agents. "
        "Identify and quarantine consistently slow or flaky tests. "
        "Evaluate test impact analysis (run only tests affected by the change).",
    ),
    "deploy": (
        "Speed up deploy / publish step",
        "Parallelize independent deployment targets. "
        "Use incremental or blue-green deployments to minimise per-release overhead.",
    ),
    "security_scan": (
        "Reduce security scan overhead",
        "Run security scans on a schedule (e.g. nightly) rather than on every PR. "
        "Scope scans to changed files only using incremental scanning options.",
    ),
    "firewall": (
        "Minimise firewall rule overhead",
        "Pre-provision stable firewall rules at provisioning time rather than per run. "
        "Batch firewall rule changes when multiple pipelines share the same rule set.",
    ),
}

_SUFFICIENCY_WEIGHT: dict[DataSufficiency, float] = {
    DataSufficiency.HIGH:   1.0,
    DataSufficiency.MEDIUM: 0.8,
    DataSufficiency.LOW:    0.5,
}


# ── Internal helpers ───────────────────────────────────────────────────────────

def _compute_confidence(
    shap_impact:      float,
    opportunity:      float,
    actual_duration:  int,
    data_sufficiency: DataSufficiency,
) -> float:
    if actual_duration <= 0:
        return 0.0
    attribution_score  = min(1.0, abs(shap_impact) / actual_duration)
    opportunity_score  = min(1.0, opportunity       / actual_duration)
    raw_confidence     = attribution_score * 0.5 + opportunity_score * 0.5
    weight             = _SUFFICIENCY_WEIGHT.get(data_sufficiency, 0.5)
    return round(raw_confidence * weight, 4)


def _priority(confidence: float) -> Priority:
    if confidence >= 0.65:
        return Priority.HIGH
    if confidence >= 0.40:
        return Priority.MEDIUM
    return Priority.LOW


def generate_narrative(
    phase:              str,
    observed_seconds:   int,
    benchmark_seconds:  int,
    opportunity_seconds:int,
    shap_impact_seconds:float,
    actual_duration:    int,
    run_context:        dict,
) -> str:
    """
    Generate a plain-English recommendation narrative.

    When LLM_ENABLED is True, calls Azure OpenAI.
    Always falls back to the deterministic template on any failure.
    """
    title, action_hint = PHASE_TEMPLATES.get(
        phase,
        ("Review pipeline phase", "Investigate this phase for optimisation opportunities."),
    )

    phase_task_context = run_context.get("phase_task_context") or {}
    phase_work_items = phase_task_context.get(phase) or []
    phase_work_items_for_prompt = phase_work_items[:3]
    phase_work_items_str = "; ".join(phase_work_items_for_prompt) if phase_work_items_for_prompt else "none"
    compact_run_context = {
        "org": run_context.get("org"),
        "project": run_context.get("project"),
        "pipeline_id": run_context.get("pipeline_id"),
        "run_id": run_context.get("run_id"),
        "pipeline_name": run_context.get("pipeline_name"),
    }

    opp_mins      = round(opportunity_seconds / 60, 1)
    observed_mins = round(observed_seconds    / 60, 1)
    bench_mins    = round(benchmark_seconds   / 60, 1)

    template_fallback = (
        f"{title}. "
        f"This phase took {observed_mins} min — {opp_mins} min above the benchmark "
        f"({bench_mins} min). "
        f"Suggested action: {action_hint}"
    )

    if not settings.LLM_ENABLED:
        return template_fallback

    try:
        openai_mod = __import__("openai")
        AzureOpenAI = getattr(openai_mod, "AzureOpenAI")

        if not settings.AZURE_OPENAI_ENDPOINT or not settings.AZURE_OPENAI_API_KEY:
            logger.warning("LLM_ENABLED=True but Azure OpenAI endpoint/key missing; using template.")
            return template_fallback

        client = AzureOpenAI(
            azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
            api_key=settings.AZURE_OPENAI_API_KEY,
            api_version=settings.AZURE_OPENAI_API_VERSION,
        )

        system_prompt = (
            "You are a CI/CD optimization assistant. Produce exactly 2 concise sentences. "
            "Sentence 1 must describe the bottleneck using the provided metrics and, when work items are provided, "
            "must mention at least one exact task, job, or stage title verbatim. "
            "Sentence 2 must provide one concrete, implementable action for this specific phase and may reference the action hint. "
            "Do not use markdown or bullets. Do not invent tools, tasks, or metrics not present in the input. "
            "Avoid generic phrases like 'optimize performance' or 'improve efficiency'."
        )
        user_prompt = (
            f"Phase: {phase}\n"
            f"Observed: {observed_mins} min\n"
            f"Benchmark: {bench_mins} min\n"
            f"Opportunity: {opp_mins} min\n"
            f"SHAP impact: {shap_impact_seconds:.0f}s\n"
            f"Top work items in this phase (task/job/stage, use exact titles if provided): {phase_work_items_str}\n"
            f"Run context: {compact_run_context}\n"
            f"Action hint: {action_hint}"
        )

        response = client.chat.completions.create(
            model=settings.AZURE_OPENAI_DEPLOYMENT,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=170,
            temperature=0.2,
        )

        content = (response.choices[0].message.content or "").strip()
        return content if content else template_fallback
    except Exception as exc:
        logger.warning("LLM narrative generation failed for phase=%s: %s — using template.", phase, exc)
        return template_fallback


# ── Public API ─────────────────────────────────────────────────────────────────

def build_recommendations(
    shap_by_feature:     dict[str, float],
    benchmark:           BenchmarkOutput,
    actual_duration:     int,
    run_context:         dict,
    top_k:               int   = 3,
    min_confidence:      float = 0.55,
    min_opportunity_sec: int   = 60,
    min_shap_impact_sec: int   = 20,
) -> list[dict]:
    """
    Build a ranked list of actionable recommendations.

    Gate: recommendation is included ONLY if ALL THREE pass:
      shap_impact  >= min_shap_impact_sec
      opportunity  >= min_opportunity_sec
      confidence   >= min_confidence

    When no recommendations pass all gates, returns [] — callers must
    handle the empty list and set actionability accordingly.
    """
    candidates: list[dict] = []

    logger.info(
        "Recommendation thresholds: min_confidence=%.2f, min_opportunity_sec=%d, "
        "min_shap_impact_sec=%d, top_k=%d",
        min_confidence,
        min_opportunity_sec,
        min_shap_impact_sec,
        top_k,
    )

    phase_map = {pb.phase: pb for pb in benchmark.phases}

    for phase, col in PHASE_FEATURES.items():
        shap_impact  = shap_by_feature.get(col, 0.0)
        phase_bench  = phase_map.get(phase)
        opportunity  = phase_bench.opportunity_seconds if phase_bench else 0

        confidence = _compute_confidence(
            shap_impact, opportunity, actual_duration, benchmark.data_sufficiency
        )

        logger.info(
            "Phase=%s metrics: shap_impact_seconds=%.1f, opportunity_seconds=%d, "
            "confidence=%.3f",
            phase,
            shap_impact,
            opportunity,
            confidence,
        )

        # Gate 1 & 2: minimum absolute thresholds
        if abs(shap_impact) < min_shap_impact_sec:
            logger.info(
                "Phase=%s rejected by SHAP gate: %.1f < %d",
                phase,
                abs(shap_impact),
                min_shap_impact_sec,
            )
            continue
        if opportunity < min_opportunity_sec:
            logger.info(
                "Phase=%s rejected by opportunity gate: %d < %d",
                phase,
                opportunity,
                min_opportunity_sec,
            )
            continue

        # Gate 3: confidence
        if confidence < min_confidence:
            logger.info(
                "Phase=%s rejected by confidence gate: %.3f < %.3f",
                phase,
                confidence,
                min_confidence,
            )
            continue

        observed   = phase_bench.observed_seconds  if phase_bench else 0
        bench_secs = phase_bench.benchmark_seconds if phase_bench else 0

        narrative = generate_narrative(
            phase=phase,
            observed_seconds=observed,
            benchmark_seconds=bench_secs,
            opportunity_seconds=opportunity,
            shap_impact_seconds=shap_impact,
            actual_duration=actual_duration,
            run_context=run_context,
        )

        candidates.append({
            "phase":              phase,
            "title":              PHASE_TEMPLATES.get(phase, ("Review phase", ""))[0],
            "narrative":          narrative,
            "shap_impact_seconds":round(shap_impact, 1),
            "opportunity_seconds":opportunity,
            "confidence":         confidence,
            "priority":           _priority(confidence).value,
            "observed_seconds":   observed,
            "benchmark_seconds":  bench_secs,
        })
        logger.info(
            "Phase=%s accepted with confidence=%.3f and opportunity_seconds=%d",
            phase,
            confidence,
            opportunity,
        )

    # Sort by confidence descending, then opportunity descending
    candidates.sort(key=lambda r: (-r["confidence"], -r["opportunity_seconds"]))
    return candidates[:top_k]


def compute_actionability(recommendations: list[dict]) -> Actionability:
    """Map recommendation list to an overall actionability label."""
    if not recommendations:
        return Actionability.LOW
    top_confidence = recommendations[0]["confidence"]
    if top_confidence >= 0.65:
        return Actionability.HIGH
    if top_confidence >= 0.40:
        return Actionability.MEDIUM
    return Actionability.LOW

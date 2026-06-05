from __future__ import annotations

import logging
import threading

from config import settings
from benchmark import BenchmarkOutput
from feature_builder import PHASE_FEATURES
from schemas import Actionability, DataSufficiency, Priority

logger = logging.getLogger(__name__)

_openai_client = None
_openai_lock = threading.Lock()


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        with _openai_lock:
            if _openai_client is None:
                openai_mod = __import__("openai")
                AzureOpenAI = getattr(openai_mod, "AzureOpenAI")
                _openai_client = AzureOpenAI(
                    azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
                    api_key=settings.AZURE_OPENAI_API_KEY,
                    api_version=settings.AZURE_OPENAI_API_VERSION,
                )
    return _openai_client

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
    raw_confidence     = attribution_score * 0.3 + opportunity_score * 0.7
    weight             = _SUFFICIENCY_WEIGHT.get(data_sufficiency, 0.5)
    confidence         = round(raw_confidence * weight, 4)
    if opportunity_score >= 0.25 and confidence < 0.65:
        confidence = 0.65   # ≥25% of run → HIGH
    elif opportunity_score >= 0.15 and confidence < 0.40:
        confidence = 0.40   # ≥15% of run → at least MEDIUM
    return confidence


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
    pipeline_name = run_context.get("pipeline_name") or "this pipeline"

    opp_mins      = round(opportunity_seconds / 60, 1)
    observed_mins = round(observed_seconds    / 60, 1)
    bench_mins    = round(benchmark_seconds   / 60, 1)
    opp_pct       = round(opportunity_seconds / actual_duration * 100, 1) if actual_duration > 0 else 0.0

    # When benchmark is zero there is no historical baseline — phrase accordingly.
    if benchmark_seconds == 0:
        benchmark_clause = "no historical baseline available for comparison"
    else:
        benchmark_clause = f"{opp_mins} min above the benchmark ({bench_mins} min)"

    template_fallback = (
        f"{title}. "
        f"This phase took {observed_mins} min — {benchmark_clause}. "
        f"Suggested action: {action_hint}"
    )

    if not settings.LLM_ENABLED:
        return template_fallback

    try:
        if not settings.AZURE_OPENAI_ENDPOINT or not settings.AZURE_OPENAI_API_KEY:
            logger.warning("LLM_ENABLED=True but Azure OpenAI endpoint/key missing; using template.")
            return template_fallback

        client = _get_openai_client()

        # Format work items as a short numbered list so the model can reference them clearly.
        if phase_work_items:
            items_block = "\n".join(
                f"  {i}. {item}" for i, item in enumerate(phase_work_items[:5], 1)
            )
            work_items_section = f"Tasks/jobs/stages observed in this phase:\n{items_block}"
        else:
            work_items_section = "Tasks/jobs/stages observed in this phase: none recorded"

        system_prompt = (
            "You are a CI/CD pipeline performance analyst writing plain-English recommendations "
            "for software engineering teams. "
            "Write 3 to 4 sentences that follow this structure:\n"
            "1. State the severity and impact: how long the phase took, how it compares to the benchmark, "
            "and what percentage of the total run it represents.\n"
            "2. Identify the specific bottleneck: if work items are listed, name at least one "
            "exact task title verbatim and state its duration. The duration shown next to each entry is "
            "the task duration, not the job or stage duration — do not attribute the task duration to the job or stage.\n"
            "3. Give one primary, immediately actionable step the team can take.\n"
            "4. (Optional) Mention a secondary action or expected benefit if space allows.\n"
            "Rules: plain prose only — no markdown, no bullet points, no numbered lists in the output. "
            "Do not invent task names, tools, or metrics not present in the input. "
            "Do not use vague phrases like 'optimize performance' or 'improve efficiency'. "
            "Calibrate confidence language to the data sufficiency level provided."
        )

        user_prompt = (
            f"Pipeline: {pipeline_name}\n"
            f"Phase: {phase}\n"
            f"Observed duration: {observed_mins} min\n"
            f"Benchmark (target): {bench_mins} min" + (" (no historical baseline — first runs for this pipeline)" if benchmark_seconds == 0 else "") + "\n"
            f"Opportunity (time to save): {opp_mins} min ({opp_pct}% of total run)\n"
            f"Total run duration: {round(actual_duration / 60, 1)} min\n"
            f"SHAP model attribution: {shap_impact_seconds:.0f}s\n"
            f"Data sufficiency: {run_context.get('data_sufficiency', 'unknown')}\n"
            f"{work_items_section}\n"
            f"Suggested action hint: {action_hint}"
        )

        response = client.chat.completions.create(
            model=settings.AZURE_OPENAI_DEPLOYMENT,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=300,
            temperature=0.2,
        )

        content = (response.choices[0].message.content or "").strip()
        return content if content else template_fallback
    except Exception as exc:
        logger.warning("LLM narrative generation failed for phase=%s: %s — using template.", phase, exc)
        return template_fallback


# ── Cross-cutting signal thresholds ──────────────────────────────────────────
# These fire based on absolute feature values, independent of benchmark data.
_DUPLICATE_TASK_THRESHOLD   = 3    # >= N duplicate task occurrences
_SKIPPED_TASK_THRESHOLD     = 5    # >= N skipped tasks
_QUEUE_WAIT_THRESHOLD_SEC   = 120  # >= 2 min queue wait
_LOW_PARALLELISM_RATIO      = 0.6  # ratio < this with job_count > 2


def _build_cross_cutting_recommendations(
    feature_values:  dict[str, float],
    shap_by_feature: dict[str, float],
    actual_duration: int,
    run_context:     dict,
) -> list[dict]:
    """
    Generate recommendations from cross-cutting feature signals that are
    independent of per-phase benchmark comparisons.

    These fire on absolute thresholds (e.g. 5 duplicate tasks) and can
    surface problems even when a pipeline has no benchmark history.
    """
    recs: list[dict] = []
    pipeline_name        = run_context.get("pipeline_name") or "this pipeline"
    cross_cutting_ctx    = run_context.get("cross_cutting_context") or {}
    named_dup_tasks      = cross_cutting_ctx.get("duplicate_tasks") or []
    named_skipped_tasks  = cross_cutting_ctx.get("skipped_tasks")  or []

    def _shap_secs(col: str) -> float:
        return abs(shap_by_feature.get(col, 0.0))

    def _val(col: str) -> float:
        return float(feature_values.get(col, 0.0))

    # 1. Duplicate tasks ───────────────────────────────────────────────────────
    dup_count = int(_val("duplicate_task_occurrences"))
    if dup_count >= _DUPLICATE_TASK_THRESHOLD:
        shap_s = _shap_secs("duplicate_task_occurrences")
        estimated_waste = min(int(shap_s), actual_duration) if shap_s > 0 else 0

        shap_fraction = min(1.0, shap_s / actual_duration) if actual_duration > 0 else 0.0
        count_signal  = min(1.0, dup_count / 20)           # saturates at 20 duplicates
        confidence    = round(count_signal * 0.4 + shap_fraction * 0.6, 4)
        if shap_fraction < 0.05 and confidence > 0.40:
            confidence = 0.40

        is_hygiene = shap_s < 60

        if settings.LLM_ENABLED and settings.AZURE_OPENAI_ENDPOINT and settings.AZURE_OPENAI_API_KEY:
            try:
                client = _get_openai_client()
                hygiene_hint = (
                    "The model attributes minimal direct duration savings to this pattern, "
                    "so frame this as a maintainability and pipeline hygiene issue rather than a speed fix."
                    if is_hygiene else ""
                )
                dup_task_list = (
                    "Top duplicated tasks: " + ", ".join(named_dup_tasks)
                    if named_dup_tasks else ""
                )
                response = client.chat.completions.create(
                    model=settings.AZURE_OPENAI_DEPLOYMENT,
                    messages=[{
                        "role": "system",
                        "content": (
                            "You are a CI/CD pipeline performance analyst. "
                            "Write 2 to 3 plain-prose sentences (no markdown, no bullets). "
                            "Describe the problem with duplicate task runs and give one concrete action. "
                            + hygiene_hint
                        )
                    }, {
                        "role": "user",
                        "content": (
                            f"Pipeline: {pipeline_name}\n"
                            f"Duplicate task occurrences: {dup_count} task rows appear more than once "
                            f"across different jobs in this run\n"
                            + (f"{dup_task_list}\n" if dup_task_list else "")
                            + f"Model SHAP total attribution for this feature: {shap_s:.0f}s "
                            f"(total predicted impact on run duration, not a per-occurrence figure)\n"
                            f"Total run duration: {round(actual_duration / 60, 1)} min\n"
                            "Action hint: Audit the YAML for tasks defined in multiple jobs that "
                            "could be extracted to a shared template or run once with artefact passing."
                        )
                    }],
                    max_tokens=180,
                    temperature=0.2,
                )
                narrative = (response.choices[0].message.content or "").strip()
            except Exception as exc:
                logger.warning("LLM failed for duplicate_tasks: %s", exc)
                narrative = ""
        else:
            narrative = ""

        if not narrative:
            hygiene_note = (
                " While direct duration savings are uncertain, reducing duplicates improves "
                "pipeline maintainability and reduces unnecessary scheduling overhead."
                if is_hygiene else ""
            )
            task_detail = (
                f" Top offenders: {', '.join(named_dup_tasks[:3])}."
                if named_dup_tasks else ""
            )
            narrative = (
                f"{dup_count} duplicate task occurrences detected in {pipeline_name} — "
                "the same task names appear in multiple jobs within a single run."
                + task_detail + hygiene_note + " "
                "Audit the pipeline YAML for tasks defined in multiple jobs that could "
                "be extracted to a shared template or run once with artefact passing."
            )

        recs.append({
            "phase":               "duplicate_tasks",
            "title":              "Eliminate duplicate task runs",
            "narrative":           narrative,
            "shap_impact_seconds": round(shap_s, 1),
            "opportunity_seconds": estimated_waste,
            "confidence":          confidence,
            "priority":            _priority(confidence).value,
            "observed_seconds":    0,
            "benchmark_seconds":   0,
        })
        logger.info("Cross-cutting: duplicate_tasks dup_count=%d confidence=%.3f", dup_count, confidence)

    # 2. Skipped tasks ─────────────────────────────────────────────────────────
    skipped = int(_val("skipped_task_count"))
    if skipped >= _SKIPPED_TASK_THRESHOLD:
        shap_s = _shap_secs("skipped_task_count")
        confidence = round(min(1.0, skipped / 20) * 0.6, 4)

        if settings.LLM_ENABLED and settings.AZURE_OPENAI_ENDPOINT and settings.AZURE_OPENAI_API_KEY:
            try:
                client = _get_openai_client()
                skipped_task_list = (
                    "Tasks consistently skipped: " + ", ".join(named_skipped_tasks)
                    if named_skipped_tasks else ""
                )
                response = client.chat.completions.create(
                    model=settings.AZURE_OPENAI_DEPLOYMENT,
                    messages=[{
                        "role": "system",
                        "content": (
                            "You are a CI/CD pipeline performance analyst. "
                            "Write 2 to 3 plain-prose sentences (no markdown, no bullets). "
                            "Explain the problem with many skipped tasks and give one concrete action."
                        )
                    }, {
                        "role": "user",
                        "content": (
                            f"Pipeline: {pipeline_name}\n"
                            f"Skipped task count: {skipped}\n"
                            + (f"{skipped_task_list}\n" if skipped_task_list else "")
                            + f"Total run duration: {round(actual_duration / 60, 1)} min\n"
                            "Action hint: Review pipeline conditions and triggers. Remove or consolidate "
                            "tasks that are consistently skipped to reduce scheduling noise and agent overhead."
                        )
                    }],
                    max_tokens=150,
                    temperature=0.2,
                )
                narrative = (response.choices[0].message.content or "").strip() or ""
            except Exception as exc:
                logger.warning("LLM failed for skipped_tasks: %s", exc)
                narrative = ""
        if not narrative:
            task_detail = (
                f" Consistently skipped: {', '.join(named_skipped_tasks[:3])}."
                if named_skipped_tasks else ""
            )
            narrative = (
                f"{skipped} tasks were scheduled but skipped during this run of {pipeline_name}."
                + task_detail + " "
                "Skipped tasks still consume scheduling overhead and make pipeline logs harder to read. "
                "Review pipeline conditions and triggers, and remove or consolidate tasks that "
                "are consistently skipped."
            )

        recs.append({
            "phase":               "skipped_tasks",
            "title":              "Remove consistently skipped tasks",
            "narrative":           narrative,
            "shap_impact_seconds": round(shap_s, 1),
            "opportunity_seconds": 0,
            "confidence":          confidence,
            "priority":            _priority(confidence).value,
            "observed_seconds":    0,
            "benchmark_seconds":   0,
        })
        logger.info("Cross-cutting: skipped_tasks count=%d confidence=%.3f", skipped, confidence)

    # 3. Low parallelism ───────────────────────────────────────────────────────
    job_count = int(_val("job_count"))
    parallelism = _val("parallelism_ratio")
    has_parallel = int(_val("has_parallel_execution"))
    if job_count > 2 and parallelism < _LOW_PARALLELISM_RATIO and not has_parallel:
        shap_s = _shap_secs("parallelism_ratio")
        # Estimate: if parallelism ratio were 0.8, how much time would be saved
        sequential_overhead = int(actual_duration * (0.8 - parallelism))
        confidence = round(min(1.0, (0.8 - parallelism)) * 0.7, 4)

        narrative = (
            f"{pipeline_name} runs {job_count} jobs sequentially (parallelism ratio: "
            f"{round(parallelism, 2)}), meaning jobs wait for each other to finish "
            f"rather than running concurrently. Enabling parallel job execution in the "
            f"pipeline YAML could reduce total run time by up to "
            f"{round(sequential_overhead / 60, 1)} min. Review job dependencies and "
            f"use \"dependsOn\" to run independent jobs simultaneously."
        )
        recs.append({
            "phase":               "parallelism",
            "title":              "Enable parallel job execution",
            "narrative":           narrative,
            "shap_impact_seconds": round(shap_s, 1),
            "opportunity_seconds": sequential_overhead,
            "confidence":          confidence,
            "priority":            _priority(confidence).value,
            "observed_seconds":    0,
            "benchmark_seconds":   0,
        })
        logger.info("Cross-cutting: low_parallelism ratio=%.2f jobs=%d confidence=%.3f", parallelism, job_count, confidence)

    # 4. High queue wait (absolute threshold, no benchmark needed) ─────────────
    queue_secs = int(_val("queue_wait_seconds"))
    if queue_secs >= _QUEUE_WAIT_THRESHOLD_SEC:
        shap_s = _shap_secs("queue_wait_seconds")
        confidence = round(min(1.0, queue_secs / actual_duration) * 0.9, 4) if actual_duration > 0 else 0.0
        queue_mins = round(queue_secs / 60, 1)
        narrative = (
            f"This run waited {queue_mins} min in the agent queue before execution began, "
            f"accounting for {round(queue_secs / actual_duration * 100, 1)}% of total pipeline time. "
            "High queue wait typically indicates agent pool saturation at peak hours. "
            "Consider increasing the agent pool size, using self-hosted agents, or staggering "
            "scheduled trigger times to spread load."
        )
        recs.append({
            "phase":               "queue",
            "title":              "Reduce agent queue wait time",
            "narrative":           narrative,
            "shap_impact_seconds": round(shap_s, 1),
            "opportunity_seconds": queue_secs,
            "confidence":          confidence,
            "priority":            _priority(confidence).value,
            "observed_seconds":    queue_secs,
            "benchmark_seconds":   0,
        })
        logger.info("Cross-cutting: high_queue_wait secs=%d confidence=%.3f", queue_secs, confidence)

    return recs


# ── Public API ─────────────────────────────────────────────────────────────────

def build_recommendations(
    shap_by_feature:     dict[str, float],
    benchmark:           BenchmarkOutput,
    actual_duration:     int,
    run_context:         dict,
    feature_values:      dict[str, float] | None = None,
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
    if len(candidates) >= top_k:
        return candidates[:top_k]

    accepted_phases = {c["phase"] for c in candidates}
    fallback_candidates = []

    for phase, col in PHASE_FEATURES.items():
        if phase in accepted_phases:
            continue
        phase_bench = phase_map.get(phase)
        opportunity = phase_bench.opportunity_seconds if phase_bench else 0
        if opportunity < min_opportunity_sec:
            continue

        shap_impact = shap_by_feature.get(col, 0.0)
        confidence  = _compute_confidence(
            shap_impact, opportunity, actual_duration, benchmark.data_sufficiency
        )
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
        fallback_candidates.append({
            "phase":              phase,
            "title":              PHASE_TEMPLATES.get(phase, ("Review phase", ""))[0],
            "narrative":          narrative,
            "shap_impact_seconds":round(shap_impact, 1),
            "opportunity_seconds":opportunity,
            "confidence":         confidence,
            "priority":           Priority.LOW.value,
            "observed_seconds":   observed,
            "benchmark_seconds":  bench_secs,
        })
        logger.info(
            "Phase=%s added via opportunity fallback: opportunity_seconds=%d confidence=%.3f",
            phase, opportunity, confidence,
        )

    fallback_candidates.sort(key=lambda r: -r["opportunity_seconds"])
    needed = top_k - len(candidates)
    phase_results = (candidates + fallback_candidates[:needed])[:top_k]

    # ── Cross-cutting signals (redundancy, parallelism, queue) ──────────────────
    # These fire on absolute thresholds regardless of benchmark availability.
    # They are appended after phase results and the combined list is re-sorted
    # and capped at top_k, so they only surface when they rank high enough.
    cross_cutting: list[dict] = []
    if feature_values:
        accepted_phases = {r["phase"] for r in phase_results}
        cross_cutting = [
            r for r in _build_cross_cutting_recommendations(
                feature_values, shap_by_feature, actual_duration, run_context
            )
            if r["phase"] not in accepted_phases  # don't duplicate queue if already in phase results
        ]

    combined = phase_results + cross_cutting
    combined.sort(key=lambda r: (-r["confidence"], -r["opportunity_seconds"]))
    return combined[:top_k]


def compute_actionability(recommendations: list[dict]) -> Actionability:
    """Map recommendation list to an overall actionability label."""
    if not recommendations:
        return Actionability.LOW
    top_confidence   = recommendations[0]["confidence"]
    any_high         = any(r["confidence"] >= 0.65 for r in recommendations)
    total_saving_sec = sum(r.get("opportunity_seconds", 0) or 0 for r in recommendations)
    if any_high or (top_confidence >= 0.40 and total_saving_sec >= 600):
        return Actionability.HIGH
    if top_confidence >= 0.40:
        return Actionability.MEDIUM
    return Actionability.LOW

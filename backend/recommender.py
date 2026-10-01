from __future__ import annotations

import logging
import threading

from config import settings
from benchmark import BenchmarkOutput

logger = logging.getLogger(__name__)

RECOMMENDATION_RULES_VERSION = "3"

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
# Each hint cites the Azure Pipelines or vendor docs it was checked against on 2026-10-01.
PHASE_TEMPLATES: dict[str, tuple[str, str]] = {
    # Docs: "Pipeline caching"
    "restore": (
        "Speed up package restore",
        "If a Cache@2 step exists, check whether it missed in this run "
        "(changed lockfile, or expired after 7 idle days). If there is none, add one keyed on "
        "the lockfile hash (NuGet PackageReference: packages.lock.json plus NUGET_PACKAGES; "
        "npm: cache npm_config_cache, not node_modules with npm ci). It only pays off when "
        "restoring and saving the cache takes less time than downloading the packages.",
    ),
    # Docs: "steps.checkout", "Publish and download pipeline artifacts"
    "download": (
        "Reduce checkout and download time",
        "If the time is in checkout: use fetchDepth: 1 (unless the build needs history) with "
        "fetchTags: false, because tags can still be synced on a shallow fetch; for large repos "
        "add fetchFilter: blob:none or a sparse checkout; use checkout: none in jobs that only "
        "need artifacts. If it is in artifact download: name the artifact, filter with patterns, "
        "and add download: none to deployment jobs that don't need the automatic artifact download.",
    ),
    # Docs: "Microsoft-hosted agents" (fresh VM per job)
    "build": (
        "Accelerate build / compile step",
        "Check whether the same solution is built in several jobs or stages; build it once and "
        "publish the output as a pipeline artifact. Incremental builds only help on self-hosted "
        "agents that keep their workspace between runs.",
    ),
    # Docs: "Use Test Impact Analysis", "Run VSTest tests in parallel"
    "test": (
        "Optimise test execution time",
        "Fix or quarantine slow and flaky tests first: they and their retries are agent time you "
        "can remove. Run tests in parallel inside each agent (framework parallelism or vstest "
        "/parallel) before adding agents; extra agents mainly shorten elapsed time, not agent time. "
        "Test Impact Analysis can skip unaffected tests, but only for VSTest v2 on managed "
        "single-machine tests (not .NET Core, multi-machine setups, or non-VSTest runners such as "
        "Jest or Cypress).",
    ),
    # Docs: "Artifacts in Azure Pipelines", PublishBuildArtifacts@1
    "deploy": (
        "Speed up deploy / publish step",
        "If the time is in publishing artifacts: use Pipeline Artifacts (publish: or "
        "PublishPipelineArtifact), which Microsoft recommends over Build Artifacts for faster "
        "performance (not available in classic release pipelines), and add a .artifactignore "
        "to leave out files nothing downstream needs. "
        "If it is in deploy tasks: deploy only what changed, and run independent targets as "
        "parallel jobs, which shortens elapsed time rather than agent time.",
    ),
    # Black Duck Polaris and Coverity PR-scan docs (seen as search snippets only)
    "security_scan": (
        "Reduce security scan overhead",
        "Use the scanner's pull-request or incremental mode on PRs (Polaris pull request scans, "
        "Coverity desktop analysis of changed files) and keep the full scan for a schedule or "
        "main; both rely on an earlier full scan as the baseline. Check any reduction in scan "
        "frequency against your security and compliance requirements.",
    ),
    # Docs: "Microsoft-hosted agents" (Networking, FAQ)
    "firewall": (
        "Minimise firewall rule overhead",
        "Hosted-agent IP ranges change weekly and can't be listed by service tag, so a permanent "
        "rule is only practical for agents with fixed addresses. Run the jobs that need the "
        "allow-list on self-hosted, scale-set or Managed DevOps Pool agents, or group those steps "
        "into fewer jobs so rules are opened, awaited and removed less often (each job can run on "
        "a different agent IP).",
    ),
}


# ── Internal helpers ───────────────────────────────────────────────────────────

def _share_pct(seconds: int, run_agent_seconds: int) -> float | None:
    return round(seconds / run_agent_seconds * 100, 1) if run_agent_seconds > 0 else None


def generate_narrative(
    phase:             str,
    observed_seconds:  int,
    typical_seconds:   int,
    run_agent_seconds: int,
    run_context:       dict,
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
    baseline_runs = run_context.get("baseline_runs") or 0

    above_seconds = max(0, observed_seconds - typical_seconds)
    observed_mins = round(observed_seconds / 60, 1)
    typical_mins  = round(typical_seconds  / 60, 1)
    above_mins    = round(above_seconds    / 60, 1)
    share_pct     = _share_pct(above_seconds, run_agent_seconds) or 0.0

    template_fallback = (
        f"{title}. "
        f"This phase used {observed_mins} agent-minutes in this run, summed across all jobs, "
        f"against a typical {typical_mins} for {pipeline_name}: {above_mins} more than usual. "
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
            "Every duration you receive is agent time: task durations added up across all jobs in "
            "the run, including jobs that ran at the same time. Agent time is not elapsed time.\n"
            "Write 3 to 4 sentences that follow this structure:\n"
            "1. State how much agent time this phase used in this run and how that compares with "
            "the typical agent time for this phase in earlier runs of the same pipeline.\n"
            "2. Identify the specific bottleneck: if work items are listed, name at least one "
            "exact task title verbatim and state its duration. The duration shown next to each entry is "
            "the task duration, not the job or stage duration — do not attribute the task duration to the job or stage.\n"
            "3. Give one primary, immediately actionable step the team can take.\n"
            "4. (Optional) Mention a secondary action if space allows.\n"
            "Rules: plain prose only — no markdown, no bullet points, no numbered lists in the output. "
            "Say 'agent time' or 'agent-minutes'; never call it elapsed time, run time, or pipeline duration. "
            "Do not state what share of the pipeline's duration a phase takes, and do not promise time or cost savings. "
            "Do not invent task names, tools, or metrics not present in the input. "
            "Do not use vague phrases like 'optimize performance' or 'improve efficiency'."
        )

        user_prompt = (
            f"Pipeline: {pipeline_name}\n"
            f"Phase: {phase}\n"
            f"Agent time for this phase in this run: {observed_mins} min\n"
            f"Typical agent time for this phase: {typical_mins} min "
            f"(median of {baseline_runs} earlier runs of this pipeline that ran the same stages)\n"
            f"Above typical: {above_mins} min ({share_pct}% of this run's agent time)\n"
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
# These fire on absolute values from this run, independent of the baseline.
_SKIPPED_TASK_THRESHOLD   = 5    # >= N skipped tasks
_QUEUE_WAIT_THRESHOLD_SEC = 120  # >= 2 min queue wait


def _build_cross_cutting_recommendations(
    feature_values:      dict[str, float],
    run_agent_seconds:   int,
    run_context:         dict,
    min_opportunity_sec: int,
) -> list[dict]:
    """
    Generate recommendations from signals measured in this run that are
    independent of the per-phase baseline, so they can surface problems
    even when a pipeline has no history to compare with.
    """
    recs: list[dict] = []
    pipeline_name        = run_context.get("pipeline_name") or "this pipeline"
    cross_cutting_ctx    = run_context.get("cross_cutting_context") or {}
    named_repeated_tasks = cross_cutting_ctx.get("repeated_tasks") or []
    named_skipped_tasks  = cross_cutting_ctx.get("skipped_tasks")  or []

    def _val(col: str) -> float:
        return float(feature_values.get(col, 0.0))

    # 1. Repeated tasks ────────────────────────────────────────────────────────
    repeated_secs = int(cross_cutting_ctx.get("repeated_seconds") or 0)
    if repeated_secs >= min_opportunity_sec:
        repeated_mins = round(repeated_secs / 60, 1)
        share_pct     = _share_pct(repeated_secs, run_agent_seconds)
        action_hint   = (
            "Cache restores (Cache@2), build once and publish a pipeline artifact, "
            "and use checkout: none in jobs that only need artifacts."
        )

        narrative = ""
        if settings.LLM_ENABLED and settings.AZURE_OPENAI_ENDPOINT and settings.AZURE_OPENAI_API_KEY:
            try:
                client = _get_openai_client()
                response = client.chat.completions.create(
                    model=settings.AZURE_OPENAI_DEPLOYMENT,
                    messages=[{
                        "role": "system",
                        "content": (
                            "You are a CI/CD pipeline performance analyst. "
                            "Write 2 to 3 plain-prose sentences (no markdown, no bullets). "
                            "Describe the repeated task executions and give one concrete action. "
                            "Durations are agent time added up across jobs, not elapsed time; "
                            "do not promise time or cost savings. Some repeats are by design "
                            "(matrix builds, a checkout in every job), so tell the team to review them."
                        )
                    }, {
                        "role": "user",
                        "content": (
                            f"Pipeline: {pipeline_name}\n"
                            f"Agent time in repeated executions (every execution after the longest one "
                            f"of the same task): {repeated_mins} min ({share_pct or 0.0}% of this run's agent time)\n"
                            + (f"Largest repeated tasks: {', '.join(named_repeated_tasks)}\n" if named_repeated_tasks else "")
                            + f"Action hint: {action_hint}"
                        )
                    }],
                    max_tokens=180,
                    temperature=0.2,
                )
                narrative = (response.choices[0].message.content or "").strip()
            except Exception as exc:
                logger.warning("LLM failed for repeated_tasks: %s", exc)

        if not narrative:
            task_detail = (
                f" Largest: {', '.join(named_repeated_tasks[:3])}."
                if named_repeated_tasks else ""
            )
            narrative = (
                f"Some tasks ran more than once in this run of {pipeline_name}, using {repeated_mins} "
                "agent-minutes beyond the longest execution of each." + task_detail + " "
                "Some repeats are by design (matrix builds, a checkout in every job), so review each one. "
                f"Typical fixes: {action_hint}"
            )

        recs.append({
            "phase":                   "repeated_tasks",
            "title":                   "Review tasks repeated across jobs",
            "narrative":               narrative,
            "figure_seconds":          repeated_secs,
            "figure_label":            "agent time in repeated tasks",
            "share_of_agent_time_pct": share_pct,
            "observed_seconds":        None,
            "typical_seconds":         None,
        })
        logger.info("Cross-cutting: repeated_tasks seconds=%d", repeated_secs)

    # 2. Skipped tasks ─────────────────────────────────────────────────────────
    skipped = int(_val("skipped_task_count"))
    if skipped >= _SKIPPED_TASK_THRESHOLD:
        narrative = ""
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
                            + "Action hint: Review pipeline conditions and triggers. Remove or consolidate "
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
            "phase":                   "skipped_tasks",
            "title":                   "Remove consistently skipped tasks",
            "narrative":               narrative,
            "figure_seconds":          None,
            "figure_label":            None,
            "share_of_agent_time_pct": None,
            "observed_seconds":        None,
            "typical_seconds":         None,
        })
        logger.info("Cross-cutting: skipped_tasks count=%d", skipped)

    # 3. High queue wait (absolute threshold, no benchmark needed) ─────────────
    queue_secs = int(_val("queue_wait_seconds"))
    if queue_secs >= _QUEUE_WAIT_THRESHOLD_SEC:
        queue_mins = round(queue_secs / 60, 1)
        narrative = (
            f"This run waited {queue_mins} min in the agent queue before any job started. "
            "High queue wait typically indicates agent pool saturation at peak hours. "
            "Consider increasing the agent pool size, using self-hosted agents, or staggering "
            "scheduled trigger times to spread load."
        )
        recs.append({
            "phase":                   "queue",
            "title":                   "Reduce agent queue wait time",
            "narrative":               narrative,
            "figure_seconds":          queue_secs,
            "figure_label":            "waiting for an agent",
            "share_of_agent_time_pct": None,
            "observed_seconds":        None,
            "typical_seconds":         None,
        })
        logger.info("Cross-cutting: high_queue_wait secs=%d", queue_secs)

    return recs


# ── Public API ─────────────────────────────────────────────────────────────────

def build_recommendations(
    benchmark:           BenchmarkOutput,
    run_context:         dict,
    feature_values:      dict[str, float],
    top_k:               int = 3,
    min_opportunity_sec: int = 60,
) -> list[dict]:
    """
    Build findings from measured agent time, largest first.

    A phase is included only when this run used more agent time on it than 9 in 10
    comparable earlier runs, and at least min_opportunity_sec more than typical.
    Returns [] when nothing qualifies.
    """
    run_agent_seconds = benchmark.observed_agent_seconds
    recs: list[dict] = []

    for pb in benchmark.phases:
        if not pb.is_unusual or pb.above_typical_seconds < min_opportunity_sec:
            continue
        recs.append({
            "phase":                   pb.phase,
            "title":                   PHASE_TEMPLATES.get(pb.phase, ("Review phase", ""))[0],
            "narrative":               generate_narrative(
                phase=pb.phase,
                observed_seconds=pb.observed_seconds,
                typical_seconds=pb.typical_seconds,
                run_agent_seconds=run_agent_seconds,
                run_context=run_context,
            ),
            "figure_seconds":          pb.above_typical_seconds,
            "figure_label":            "agent time above typical",
            "share_of_agent_time_pct": _share_pct(pb.above_typical_seconds, run_agent_seconds),
            "observed_seconds":        pb.observed_seconds,
            "typical_seconds":         pb.typical_seconds,
        })
        logger.info(
            "Phase=%s flagged: observed=%d typical=%d p90=%d",
            pb.phase, pb.observed_seconds, pb.typical_seconds, pb.p90_seconds,
        )

    recs.extend(_build_cross_cutting_recommendations(
        feature_values, run_agent_seconds, run_context, min_opportunity_sec,
    ))
    recs.sort(key=lambda r: -(r["figure_seconds"] or 0))
    return recs[:top_k]

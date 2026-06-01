from __future__ import annotations

import logging
import traceback
from datetime import datetime, timezone

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import ado_client
import benchmark as bm
import database as db
import inference
import recommender
from auth import validate_token
from config import settings
from feature_builder import PHASE_FEATURES
from schemas import (
    AnalyzeRequest,
    AnalyzeResponse,
    AnalysisResult,
    AnalysisStatus,
    BenchmarkResult,
    DecisionSummary,
    DiagnosisResult,
    ErrorDetail,
    ErrorResponse,
    HealthResponse,
    OpportunityByPhase,
    Recommendation,
    RecommendationsOnly,
    RunMetrics,
    TopContributor,
    Versions,
    # ADO browsing
    AdoOrganizationsResponse,
    AdoProjectsResponse,
    AdoPipelinesResponse,
    AdoRunsResponse,
    AdoOrganization,
    AdoProject,
    AdoPipeline,
    AdoRun,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Pipeline Analyser API",
    version="1.0.0",
    description="XGBoost + SHAP-powered CI/CD pipeline optimisation API",
)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Startup: pre-warm model ───────────────────────────────────────────────────
@app.on_event("startup")
async def _startup() -> None:
    try:
        inference.get_model()
        inference.get_explainer()
        logger.info("Model and SHAP explainer loaded successfully.")
    except Exception as exc:
        logger.error("Model failed to load at startup: %s", exc)


# ── Global error handler ──────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled error on %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            error=ErrorDetail(
                code="INTERNAL_ERROR",
                message="An unexpected error occurred. Check server logs.",
            )
        ).model_dump(),
    )


# ── Background worker ─────────────────────────────────────────────────────────

def _run_analysis(analysis_id: int, req: AnalyzeRequest, raw_token: str) -> None:
    """
    Full analysis pipeline executed asynchronously via BackgroundTasks.
    Updates DB on every status transition so the polling endpoint reflects progress.
    """
    try:
        db.update_analysis(analysis_id, "processing")

        # Step 1 — Fetch run from ADO
        try:
            ado_data = ado_client.fetch_run_data(
                org=req.org,
                project=req.project,
                pipeline_id=req.pipeline_id,
                run_id=req.run_id,
                user_token=raw_token,
            )
        except NotImplementedError:
            logger.warning(
                "ado_client.fetch_run_data not implemented; "
                "assuming run %d already exists in DB.",
                req.run_id,
            )
            ado_data = None

        if ado_data is not None:
            try:
                ado_client.store_run_data(
                    req.run_id, ado_data["run"], ado_data["timeline"]
                )
            except NotImplementedError:
                logger.warning("store_run_data not implemented; skipping persistence.")

        # Step 3 — Build features
        features_df, actual_duration, phase_task_context = bm_features = _step_features(req.run_id)

        # Step 4 — XGBoost + SHAP
        infer = inference.run_inference(
            features_df, actual_duration, top_k=req.top_k_recommendations
        )

        # Step 5 — Benchmark
        pipeline_name = db.get_pipeline_name(req.run_id)
        project_name  = db.get_project_name(req.run_id)

        observed_phase = {
            col: int(features_df[col].iloc[0])
            for col in PHASE_FEATURES.values()
            if col in features_df.columns
        }

        bench = bm.compute_benchmark(
            run_id=req.run_id,
            pipeline_name=pipeline_name,
            project=project_name,
            org=req.org,
            scope=req.comparison_scope,
            policy=req.benchmark_policy,
            observed_features=observed_phase,
            actual_duration=actual_duration,
        )

        logger.info(
            "Benchmark summary: policy=%s scope=%s sample_size=%d sufficiency=%s "
            "fallback=%s target_total_seconds=%d total_opportunity_seconds=%d phases=%d",
            bench.policy_used,
            bench.scope_used,
            bench.sample_size,
            bench.data_sufficiency,
            bench.fallback_used,
            bench.target_total_seconds,
            bench.total_opportunity_seconds,
            len(bench.phases),
        )

        # Step 6 — Recommendations
        run_context = {
            "org":           req.org,
            "project":       req.project,
            "pipeline_id":   req.pipeline_id,
            "run_id":        req.run_id,
            "pipeline_name": pipeline_name,
            "phase_task_context": phase_task_context,
        }
        recs = recommender.build_recommendations(
            shap_by_feature=infer["shap_by_feature"],
            benchmark=bench,
            actual_duration=actual_duration,
            run_context=run_context,
            top_k=req.top_k_recommendations,
            min_confidence=req.min_confidence,
            min_opportunity_sec=req.min_opportunity_seconds,
            min_shap_impact_sec=req.min_shap_impact_seconds,
        )
        actionability = recommender.compute_actionability(recs)

        # Step 7 — Build result and persist
        result = _build_result(req, actual_duration, infer, bench, recs, actionability)
        result.analysis_id = analysis_id
        db.update_analysis(analysis_id, "complete", result=result.model_dump())

    except Exception as exc:
        logger.error("Analysis %d failed: %s", analysis_id, exc, exc_info=True)
        db.update_analysis(analysis_id, "failed", error=str(exc))


def _step_features(run_id: int):
    """Thin wrapper so the error message is clear in logs."""
    from feature_builder import build_features, get_phase_task_context

    features_df, actual_duration = build_features(run_id)
    phase_task_context = get_phase_task_context(run_id)
    return features_df, actual_duration, phase_task_context


def _status_from_db(db_status: str) -> AnalysisStatus:
    """Map persisted DB status values to API enum values."""
    normalized = (db_status or "").strip().lower()
    if normalized == "processing":
        return AnalysisStatus.RUNNING
    return AnalysisStatus(normalized)


def _build_result(
    req:           AnalyzeRequest,
    actual_dur:    int,
    infer:         dict,
    bench:         bm.BenchmarkOutput,
    recs:          list[dict],
    actionability,
) -> AnalysisResult:
    run_metrics = RunMetrics(
        actual_duration_seconds=actual_dur,
        predicted_expected_seconds=infer["predicted_expected_seconds"],
        expected_gap_seconds=infer["expected_gap_seconds"],
        expected_gap_pct=infer["expected_gap_pct"],
    )

    top_contributors = [
        TopContributor(
            feature=c["feature"],
            label=c["label"],
            feature_value_seconds=int(round(abs(float(c.get("shap_seconds", 0.0) or 0.0)))),
            shap_impact_seconds=int(round(float(c.get("shap_seconds", 0.0) or 0.0))),
        )
        for c in infer["top_contributors"]
    ]

    diagnosis = DiagnosisResult(
        top_contributors=top_contributors,
    )

    phases_out = [
        OpportunityByPhase(
            phase=p.phase,
            observed_seconds=p.observed_seconds,
            benchmark_seconds=p.benchmark_seconds,
            opportunity_seconds=p.opportunity_seconds,
        )
        for p in bench.phases
    ]

    benchmark_out = BenchmarkResult(
        policy_used=bench.policy_used,
        scope_used=bench.scope_used,
        target_total_seconds=bench.target_total_seconds,
        total_opportunity_seconds=bench.total_opportunity_seconds,
        total_opportunity_pct=bench.total_opportunity_pct,
        sample_size=bench.sample_size,
        data_sufficiency=bench.data_sufficiency,
        fallback_used=bench.fallback_used,
    )

    recommendations_out = []
    for idx, r in enumerate(recs, start=1):
        savings_seconds = int(round(float(r.get("opportunity_seconds", 0) or 0)))
        observed_phase_seconds = int(round(float(r.get("observed_seconds", 0) or 0)))

        pct_of_run_raw = (savings_seconds / actual_dur * 100) if actual_dur > 0 else 0.0
        pct_of_run = round(min(100.0, max(0.0, pct_of_run_raw)), 1)
        pct_of_phase = (
            round(min(100.0, max(0.0, savings_seconds / observed_phase_seconds * 100)), 1)
            if observed_phase_seconds > 0 else 0.0
        )

        recommendations_out.append(
            Recommendation(
                id=f"rec-{req.run_id}-{idx}",
                title=r["title"],
                description=r["narrative"],
                reason_codes=[f"phase:{r['phase']}"],
                estimated_savings_seconds=savings_seconds,
                estimated_savings_pct=pct_of_run,
                estimated_savings_pct_of_run=pct_of_run,
                estimated_savings_pct_of_phase=pct_of_phase,
                confidence=r["confidence"],
                priority=r["priority"],
            )
        )

    summary = DecisionSummary(
        actionability=actionability,
        message=(
            "No strong opportunities detected for this run."
            if not recommendations_out
            else f"{len(recommendations_out)} recommendation(s) generated."
        ),
    )

    versions = Versions(
        api_version="1.0",
        model_version=settings.MODEL_VERSION,
        feature_schema_version="1",
        benchmark_policy_version="1",
        recommendation_rules_version="1",
    )

    return AnalysisResult(
        analysis_id=0,
        status=AnalysisStatus.COMPLETE,
        input={
            "org": req.org,
            "project": req.project,
            "pipeline_id": req.pipeline_id,
            "run_id": req.run_id,
        },
        run_metrics=run_metrics,
        diagnosis=diagnosis,
        benchmark=benchmark_out,
        opportunity_by_phase=phases_out,
        recommendations=recommendations_out,
        decision_summary=summary,
        versions=versions,
        completed_at_utc=datetime.now(timezone.utc).isoformat(),
    )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/v1/analyses", response_model=AnalyzeResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_analysis(
    req:             AnalyzeRequest,
    background_tasks:BackgroundTasks,
) -> AnalyzeResponse:
    """Trigger an async analysis for a pipeline run. Returns analysis_id immediately."""
    requested_by = "capstone-user"

    analysis_id, created_new = db.create_analysis(
        org=req.org,
        project=req.project,
        pipeline_id=req.pipeline_id,
        run_id=req.run_id,
        requested_by=requested_by,
        request_payload=req.model_dump(),
    )

    if created_new:
        background_tasks.add_task(_run_analysis, analysis_id, req, "")

    existing_status = AnalysisStatus.PENDING
    message = "Analysis queued. Poll GET /v1/analyses/{id} for results."
    if not created_new:
        existing = db.get_analysis(analysis_id)
        if existing:
            existing_status = _status_from_db(existing.get("status") or "pending")
        message = "Analysis already exists for this run_id. Returning existing analysis."

    return AnalyzeResponse(
        analysis_id=analysis_id,
        status=existing_status,
        message=message,
        poll_url=f"/v1/analyses/{analysis_id}",
    )


@app.get("/v1/analyses/{analysis_id}", response_model=AnalyzeResponse)
async def get_analysis(
    analysis_id: int,
) -> AnalyzeResponse:
    """Poll analysis status."""
    row = db.get_analysis(analysis_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Analysis {analysis_id} not found.")

    status_value = _status_from_db(row["status"])
    message = row.get("error_message")
    if not message:
        if status_value == AnalysisStatus.PENDING:
            message = "Analysis queued."
        elif status_value == AnalysisStatus.RUNNING:
            message = "Analysis is in progress."
        elif status_value == AnalysisStatus.COMPLETE:
            message = "Analysis complete."
        else:
            message = "Analysis failed."

    return AnalyzeResponse(
        analysis_id=row["analysis_id"],
        status=status_value,
        message=message,
        poll_url=f"/v1/analyses/{analysis_id}",
    )


@app.get("/v1/analyses/{analysis_id}/recommendations", response_model=RecommendationsOnly)
async def get_recommendations(
    analysis_id: int,
) -> RecommendationsOnly:
    """Return only the recommendations from a completed analysis."""
    row = db.get_analysis(analysis_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Analysis {analysis_id} not found.")
    if row["status"] != "complete":
        raise HTTPException(
            status_code=409,
            detail=f"Analysis is {row['status']}. Try again when status == 'complete'.",
        )

    result = row.get("result", {}) or {}
    recs   = result.get("recommendations", []) or []
    actual_duration = int((result.get("run_metrics") or {}).get("actual_duration_seconds") or 0)
    phase_observed_map = {
        str(p.get("phase")): int(p.get("observed_seconds") or 0)
        for p in (result.get("opportunity_by_phase") or [])
        if p.get("phase") is not None
    }

    mapped_recs: list[Recommendation] = []
    for idx, r in enumerate(recs, start=1):
        # Stored recommendations are already Recommendation objects serialized to dicts
        # with fields: id, title, description, reason_codes, priority, confidence, 
        # estimated_savings_seconds, estimated_savings_pct
        mapped_recs.append(
            Recommendation(
                id=r.get("id") or f"rec-{analysis_id}-{idx}",
                title=str(r.get("title") or "Recommendation"),
                description=str(r.get("description") or ""),
                reason_codes=r.get("reason_codes") or ["phase:unknown"],
                priority=str(r.get("priority") or "low"),
                confidence=float(r.get("confidence") or 0.0),
                estimated_savings_seconds=int(r.get("estimated_savings_seconds") or 0),
                estimated_savings_pct=float(
                    r.get("estimated_savings_pct_of_run")
                    if r.get("estimated_savings_pct_of_run") is not None
                    else r.get("estimated_savings_pct")
                    or 0.0
                ),
                estimated_savings_pct_of_run=float(
                    r.get("estimated_savings_pct_of_run")
                    if r.get("estimated_savings_pct_of_run") is not None
                    else r.get("estimated_savings_pct")
                    or 0.0
                ),
                estimated_savings_pct_of_phase=float(
                    r.get("estimated_savings_pct_of_phase")
                    if r.get("estimated_savings_pct_of_phase") is not None
                    else (
                        round(
                            min(
                                100.0,
                                max(
                                    0.0,
                                    (int(r.get("estimated_savings_seconds") or 0)
                                     / phase_observed_map.get(
                                         (r.get("reason_codes") or ["phase:unknown"])[0].split(":", 1)[1],
                                         0,
                                     )
                                     * 100),
                                ),
                            ),
                            1,
                        )
                        if phase_observed_map.get(
                            (r.get("reason_codes") or ["phase:unknown"])[0].split(":", 1)[1],
                            0,
                        ) > 0
                        else 0.0
                    )
                ),
            )
        )

    actionability = (result.get("decision_summary") or {}).get("actionability", "low")

    return RecommendationsOnly(
        analysis_id=analysis_id,
        status=AnalysisStatus.COMPLETE,
        recommendations=mapped_recs,
        decision_summary=DecisionSummary(
            actionability=actionability,
            message="Top recommendations for this completed analysis.",
        ),
    )


# ── ADO browsing endpoints ─────────────────────────────────────────────────────
# These power the frontend org → project → pipeline → run navigation tree.
# All four proxy to the ADO REST API via the user's OBO token; no local DB reads.

@app.get("/v1/ado/organizations", response_model=AdoOrganizationsResponse)
async def list_organizations(
) -> AdoOrganizationsResponse:
    """
    Return all ADO organizations accessible to the authenticated user.
    Frontend calls this first after login to populate the org selector.
    """
    try:
        orgs = ado_client.list_organizations(user_token="")
    except Exception as exc:
        logger.error("list_organizations failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"ADO API error: {exc}")

    return AdoOrganizationsResponse(
        organizations=[AdoOrganization(**o) for o in orgs]
    )


@app.get("/v1/ado/{org}/projects", response_model=AdoProjectsResponse)
async def list_projects(
    org:     str,
) -> AdoProjectsResponse:
    """
    Return all projects in an organization.
    Frontend calls this when the user clicks an organization.
    """
    try:
        projects = ado_client.list_projects(org, user_token="")
    except Exception as exc:
        logger.error("list_projects(%s) failed: %s", org, exc)
        raise HTTPException(status_code=502, detail=f"ADO API error: {exc}")

    return AdoProjectsResponse(
        org=org,
        projects=[AdoProject(**p) for p in projects],
    )


@app.get("/v1/ado/{org}/{project}/pipelines", response_model=AdoPipelinesResponse)
async def list_pipelines(
    org:     str,
    project: str,
) -> AdoPipelinesResponse:
    """
    Return all pipelines in a project.
    Frontend calls this when the user clicks a project.
    """
    try:
        pipelines = ado_client.list_pipelines(org, project, user_token="")
    except Exception as exc:
        logger.error("list_pipelines(%s/%s) failed: %s", org, project, exc)
        raise HTTPException(status_code=502, detail=f"ADO API error: {exc}")

    return AdoPipelinesResponse(
        org=org,
        project=project,
        pipelines=[AdoPipeline(**p) for p in pipelines],
    )


@app.get("/v1/ado/{org}/{project}/pipelines/{pipeline_id}/runs", response_model=AdoRunsResponse)
async def list_runs(
    org:         str,
    project:     str,
    pipeline_id: int,
    top:         int  = 50,
) -> AdoRunsResponse:
    """
    Return the most recent runs for a pipeline (default: last 50).
    Frontend renders these as rows, each with an Analyze button.
    """
    try:
        runs = ado_client.list_runs(org, project, pipeline_id, user_token="", top=top)
    except Exception as exc:
        logger.error("list_runs(%s/%s/%d) failed: %s", org, project, pipeline_id, exc)
        raise HTTPException(status_code=502, detail=f"ADO API error: {exc}")

    return AdoRunsResponse(
        org=org,
        project=project,
        pipeline_id=pipeline_id,
        runs=[AdoRun(**r) for r in runs],
    )


@app.get("/v1/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness + readiness check (no auth required)."""
    db_ok    = db.ping_db()
    model_ok = inference.is_model_loaded()
    ready    = db_ok and model_ok

    return HealthResponse(
        status="ready" if ready else "degraded",
        model_loaded=model_ok,
        db_reachable=db_ok,
    )

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import ado_client
import benchmark as bm
import database as db
import inference
import insights
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
    FallbackUsed,
    HealthResponse,
    InsightsProjectsResponse,
    InsightsResponse,
    PhaseComparison,
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


# ── Startup / shutdown lifecycle ──────────────────────────────────────────────
@asynccontextmanager
async def _lifespan(app: FastAPI):
    try:
        db.init_pool()
        logger.info("DB connection pool initialised (%d connections).", 5)
    except Exception as exc:
        logger.error("DB pool failed to initialise: %s", exc)
    try:
        inference.get_model()
        inference.get_explainer()
        logger.info("Model and SHAP explainer loaded successfully.")
    except Exception as exc:
        logger.error("Model failed to load at startup: %s", exc)
    yield


app = FastAPI(
    title="Pipeline Analyzer API",
    version="1.0.0",
    description="XGBoost + SHAP-powered CI/CD pipeline optimisation API",
    lifespan=_lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
_cors_origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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
        features_df, actual_duration, phase_task_context, cross_cutting_context = bm_features = _step_features(req.run_id)

        # Step 4 — XGBoost + SHAP
        infer = inference.run_inference(
            features_df, actual_duration, top_k=req.top_k_recommendations
        )

        # Step 5 — Benchmark
        pipeline_name = db.get_pipeline_name(req.run_id)
        feature_row   = features_df.iloc[0]

        bench = bm.compute_benchmark(
            run_id=req.run_id,
            pipeline_name=pipeline_name,
            observed_features={col: int(feature_row[col]) for col in PHASE_FEATURES.values()},
            observed_agent_seconds=int(feature_row["total_timeline_seconds"]),
        )

        logger.info(
            "Benchmark summary: policy=%s sample_size=%d sufficiency=%s fallback=%s "
            "observed_agent_seconds=%d typical_agent_seconds=%d phases=%d",
            bench.policy_used,
            bench.sample_size,
            bench.data_sufficiency,
            bench.fallback_used,
            bench.observed_agent_seconds,
            bench.typical_agent_seconds,
            len(bench.phases),
        )

        # Step 6 — Recommendations
        run_context = {
            "org":              req.org,
            "project":          req.project,
            "pipeline_id":      req.pipeline_id,
            "run_id":           req.run_id,
            "pipeline_name":    pipeline_name,
            "phase_task_context":     phase_task_context,
            "cross_cutting_context":  cross_cutting_context,
            "baseline_runs":    bench.sample_size,
        }
        recs = recommender.build_recommendations(
            benchmark=bench,
            run_context=run_context,
            feature_values=feature_row.to_dict(),
            top_k=req.top_k_recommendations,
            min_opportunity_sec=req.min_opportunity_seconds,
        )

        # Step 7 — Build result and persist
        result = _build_result(req, actual_duration, infer, bench, recs)
        result.analysis_id = analysis_id
        db.update_analysis(analysis_id, "complete", result=result.model_dump())

    except Exception as exc:
        logger.error("Analysis %d failed: %s", analysis_id, exc, exc_info=True)
        db.update_analysis(analysis_id, "failed", error=str(exc))


def _step_features(run_id: int):
    """Thin wrapper so the error message is clear in logs."""
    from feature_builder import build_features, get_phase_task_context, get_cross_cutting_task_context

    features_df, actual_duration = build_features(run_id)
    phase_task_context = get_phase_task_context(run_id)
    cross_cutting_context = get_cross_cutting_task_context(run_id)
    return features_df, actual_duration, phase_task_context, cross_cutting_context


def _status_from_db(db_status: str) -> AnalysisStatus:
    """Map persisted DB status values to API enum values."""
    normalized = (db_status or "").strip().lower()
    if normalized == "processing":
        return AnalysisStatus.RUNNING
    return AnalysisStatus(normalized)


def _is_current(analysis: dict) -> bool:
    """False when an analysis failed or was produced by older benchmark or recommendation rules."""
    if analysis.get("status") == "failed":
        return False
    if analysis.get("status") != "complete":
        return True
    versions = (analysis.get("result") or {}).get("versions") or {}
    return (
        versions.get("benchmark_policy_version") == bm.BENCHMARK_POLICY_VERSION
        and versions.get("recommendation_rules_version") == recommender.RECOMMENDATION_RULES_VERSION
    )


def _current_result(analysis_id: int) -> dict:
    """Stored result of a completed, up-to-date analysis; raises 404 or 409 otherwise."""
    row = db.get_analysis(analysis_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Analysis {analysis_id} not found.")
    if row["status"] != "complete":
        raise HTTPException(
            status_code=409,
            detail=f"Analysis is {row['status']}. Try again when status == 'complete'.",
        )
    if not _is_current(row):
        raise HTTPException(
            status_code=409,
            detail="This analysis was produced by an earlier version. Analyze the run again to refresh it.",
        )
    return row.get("result") or {}


def _build_result(
    req:        AnalyzeRequest,
    actual_dur: int,
    infer:      dict,
    bench:      bm.BenchmarkOutput,
    recs:       list[dict],
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
        PhaseComparison(
            phase=p.phase,
            observed_seconds=p.observed_seconds,
            typical_seconds=p.typical_seconds,
            p90_seconds=p.p90_seconds,
            above_typical_seconds=p.above_typical_seconds,
        )
        for p in bench.phases
    ]

    benchmark_out = BenchmarkResult(
        policy_used=bench.policy_used,
        scope_used=bench.scope_used,
        sample_size=bench.sample_size,
        data_sufficiency=bench.data_sufficiency,
        fallback_used=bench.fallback_used,
        observed_agent_seconds=bench.observed_agent_seconds,
        typical_agent_seconds=bench.typical_agent_seconds,
    )

    recommendations_out = [
        Recommendation(
            id=f"rec-{req.run_id}-{idx}",
            title=r["title"],
            description=r["narrative"],
            reason_codes=[f"phase:{r['phase']}"],
            figure_seconds=r["figure_seconds"],
            figure_label=r["figure_label"],
            share_of_agent_time_pct=r["share_of_agent_time_pct"],
            observed_seconds=r["observed_seconds"],
            typical_seconds=r["typical_seconds"],
        )
        for idx, r in enumerate(recs, start=1)
    ]

    if recommendations_out:
        message = f"{len(recommendations_out)} finding(s) for this run."
    elif bench.fallback_used == FallbackUsed.BENCHMARK_DISABLED:
        message = "Not enough earlier runs with the same stages to compare this run."
    else:
        message = "Nothing unusual: no phase used more agent time than 9 in 10 comparable earlier runs."
    summary = DecisionSummary(message=message)

    versions = Versions(
        api_version="1.0",
        model_version=settings.MODEL_VERSION,
        feature_schema_version="1",
        benchmark_policy_version=bm.BENCHMARK_POLICY_VERSION,
        recommendation_rules_version=recommender.RECOMMENDATION_RULES_VERSION,
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
        phase_comparison=phases_out,
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
    request:         Request,
    claims:          dict = Depends(validate_token),
) -> AnalyzeResponse:
    """Trigger an async analysis for a pipeline run. Returns analysis_id immediately."""
    requested_by = (
        claims.get("preferred_username")
        or claims.get("upn")
        or claims.get("sub")
        or "unknown"
    )
    raw_token = getattr(request.state, "raw_token", "")

    analysis_id, created_new = db.create_analysis(
        org=req.org,
        project=req.project,
        pipeline_id=req.pipeline_id,
        run_id=req.run_id,
        requested_by=requested_by,
        request_payload=req.model_dump(),
    )

    existing = None if created_new else db.get_analysis(analysis_id)
    if existing and not _is_current(existing):
        db.reset_analysis(analysis_id, requested_by=requested_by, request_payload=req.model_dump())
        existing = None

    if existing is None:
        background_tasks.add_task(_run_analysis, analysis_id, req, raw_token)
        existing_status = AnalysisStatus.PENDING
        message = "Analysis queued. Poll GET /v1/analyses/{id} for results."
    else:
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
    _claims: dict = Depends(validate_token),
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
    _claims: dict = Depends(validate_token),
) -> RecommendationsOnly:
    """Return only the recommendations from a completed analysis."""
    result = _current_result(analysis_id)
    summary = result.get("decision_summary")
    return RecommendationsOnly(
        analysis_id=analysis_id,
        status=AnalysisStatus.COMPLETE,
        recommendations=[Recommendation(**r) for r in result.get("recommendations") or []],
        decision_summary=DecisionSummary(**summary) if summary else None,
    )


@app.get("/v1/analyses/{analysis_id}/result", response_model=AnalysisResult)
async def get_analysis_result(
    analysis_id: int,
    _claims: dict = Depends(validate_token),
) -> AnalysisResult:
    """Return the full result of a completed analysis, including the per-phase comparison."""
    return AnalysisResult(**_current_result(analysis_id))


# ── ADO browsing endpoints ─────────────────────────────────────────────────────
# These power the frontend org → project → pipeline → run navigation tree.
# All four proxy to the ADO REST API via the user's OBO token; no local DB reads.

@app.get("/v1/ado/organizations", response_model=AdoOrganizationsResponse)
async def list_organizations(
    request: Request,
    _claims: dict = Depends(validate_token),
) -> AdoOrganizationsResponse:
    """
    Return all ADO organizations accessible to the authenticated user.
    Frontend calls this first after login to populate the org selector.
    """
    raw_token = getattr(request.state, "raw_token", "")
    try:
        orgs = ado_client.list_organizations(user_token=raw_token)
    except Exception as exc:
        logger.error("list_organizations failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"ADO API error: {exc}")

    return AdoOrganizationsResponse(
        organizations=[AdoOrganization(**o) for o in orgs]
    )


@app.get("/v1/ado/{org}/projects", response_model=AdoProjectsResponse)
async def list_projects(
    org:     str,
    request: Request,
    _claims: dict = Depends(validate_token),
) -> AdoProjectsResponse:
    """
    Return all projects in an organization.
    Frontend calls this when the user clicks an organization.
    """
    raw_token = getattr(request.state, "raw_token", "")
    try:
        projects = ado_client.list_projects(org, user_token=raw_token)
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
    request: Request,
    _claims: dict = Depends(validate_token),
) -> AdoPipelinesResponse:
    """
    Return all pipelines in a project.
    Frontend calls this when the user clicks a project.
    """
    raw_token = getattr(request.state, "raw_token", "")
    try:
        pipelines = ado_client.list_pipelines(org, project, user_token=raw_token)
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
    request:     Request,
    top:         int  = Query(default=50, ge=1, le=200),
    _claims:     dict = Depends(validate_token),
) -> AdoRunsResponse:
    """
    Return the most recent runs for a pipeline (default: last 50, max: 200).
    Frontend renders these as rows, each with an Analyze button.
    """
    raw_token = getattr(request.state, "raw_token", "")
    try:
        runs = ado_client.list_runs(org, project, pipeline_id, user_token=raw_token, top=top)
    except Exception as exc:
        logger.error("list_runs(%s/%s/%d) failed: %s", org, project, pipeline_id, exc)
        raise HTTPException(status_code=502, detail=f"ADO API error: {exc}")

    return AdoRunsResponse(
        org=org,
        project=project,
        pipeline_id=pipeline_id,
        runs=[AdoRun(**r) for r in runs],
    )


# ── Insights ───────────────────────────────────────────────────────────────────

# Sync on purpose: FastAPI runs it in a worker thread, so the slow SQL doesn't block the event loop.
@app.get("/v1/insights", response_model=InsightsResponse)
def read_insights(
    project: str | None = Query(default=None, max_length=100),
    days:    int | None = Query(default=None, ge=1, le=3650),
    top:     int        = Query(default=10, ge=1, le=50),
    _claims: dict       = Depends(validate_token),
) -> InsightsResponse:
    """Pipeline efficiency across stored runs: where agent time goes and what is worth reviewing."""
    return InsightsResponse(**insights.get_insights(project=project, days=days, top_n=top))


@app.get("/v1/insights/projects", response_model=InsightsProjectsResponse)
def read_insight_projects(_claims: dict = Depends(validate_token)) -> InsightsProjectsResponse:
    """Projects with stored runs, for the Pipeline Efficiency project picker."""
    return InsightsProjectsResponse(projects=insights.get_projects())


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

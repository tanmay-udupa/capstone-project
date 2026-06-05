from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pandas as pd
import shap

from config import settings

# ── Module-level singletons ────────────────────────────────────────────────────
_MODEL:     object | None = None
_EXPLAINER: object | None = None
_lock = threading.Lock()


def get_model() -> object:
    global _MODEL
    if _MODEL is None:
        with _lock:
            if _MODEL is None:
                path = Path(settings.MODEL_PATH)
                if not path.exists():
                    raise RuntimeError(
                        f"Model not found at {path}. "
                        "Copy xgb_best_model.ubj from the training output into backend/models/."
                    )
                from xgboost import XGBRegressor
                _m = XGBRegressor()
                _m.load_model(str(path))
                _MODEL = _m
    return _MODEL


def get_explainer() -> shap.TreeExplainer:
    global _EXPLAINER
    if _EXPLAINER is None:
        with _lock:
            if _EXPLAINER is None:
                _EXPLAINER = shap.TreeExplainer(get_model())
    return _EXPLAINER


def is_model_loaded() -> bool:
    """Liveness check used by /v1/health."""
    try:
        get_model()
        return True
    except Exception:
        return False


# ── Human-readable feature labels ─────────────────────────────────────────────
FEATURE_LABELS: dict[str, str] = {
    "queue_wait_seconds":       "Queue wait",
    "result_failed":            "Run result: failed",
    "result_partial":           "Run result: partial",
    "is_scheduled":             "Scheduled trigger",
    "is_main_branch":           "Main branch run",
    "job_count":                "Job count",
    "failed_job_count":         "Failed jobs",
    "max_job_duration_seconds": "Slowest job duration",
    "avg_job_duration_seconds": "Average job duration",
    "stage_count":              "Stage count",
    "failed_stage_count":       "Failed stages",
    "task_count":               "Task count",
    "failed_task_count":        "Failed tasks",
    "skipped_task_count":       "Skipped tasks",
    "restore_task_seconds":     "Package restore time",
    "download_task_seconds":    "Artifact download time",
    "build_task_seconds":       "Build / compile time",
    "test_task_seconds":        "Test execution time",
    "deploy_task_seconds":      "Deploy / publish time",
    "security_scan_seconds":    "Security scan time",
    "firewall_overhead_seconds":"Firewall overhead",
    "unique_task_count":        "Unique task types",
    "duplicate_task_occurrences":"Duplicate task runs",
    "total_tests":              "Total test cases",
    "failed_test_count":        "Failed test cases",
    "skipped_test_count":       "Skipped test cases",
    "test_pass_rate":           "Test pass rate",
    "dependency_restore_seconds":"Dependency restore time (timeline)",
    "download_seconds":         "Download time (timeline)",
    "compile_seconds":          "Compile time (timeline)",
    "parallel_job_count":       "Parallel job count",
    "total_timeline_seconds":   "Total timeline seconds",
    "parallelism_ratio":        "Parallelism ratio",
    "has_parallel_execution":   "Has parallel execution",
    "has_test_results":         "Has test results",
}


# ── Public inference API ───────────────────────────────────────────────────────

def run_inference(
    features_df:     pd.DataFrame,
    actual_duration: int,
    top_k:           int = 5,
) -> dict:
    """
    Run XGBoost prediction + SHAP attribution for a single pipeline run.

    Returns a dict with:
      predicted_expected_seconds  — model output converted from log1p scale
      expected_gap_seconds        — how far actual is from the model's expectation
      expected_gap_pct            — gap as % of actual duration
      top_contributors            — list of {feature, label, shap_seconds, pct_of_duration}
      shap_by_feature             — full {feature: shap_seconds} map
      log_prediction              — raw log-scale prediction (diagnostic)
    """
    model    = get_model()
    explainer = get_explainer()

    X = features_df.to_numpy()  # shape (1, 35)

    # ── Prediction (log1p scale → seconds) ────────────────────────────────────
    log_pred      = float(model.predict(X)[0])
    predicted_sec = max(0, int(np.expm1(log_pred)))
    gap_sec       = actual_duration - predicted_sec
    gap_pct       = round(gap_sec / actual_duration * 100, 1) if actual_duration > 0 else 0.0

    # ── SHAP attribution ───────────────────────────────────────────────────────
    shap_vals = explainer.shap_values(X)[0]  # shape (35,), log-scale

    # Convert log-scale SHAP → approximate seconds
    # Intuition: each feature's share of the log prediction scales to seconds.
    # Avoid divide-by-zero when log_pred == 0 (very short runs).
    log_abs = abs(log_pred) if abs(log_pred) > 1e-9 else 1e-9

    feature_cols  = features_df.columns.tolist()
    shap_seconds: dict[str, float] = {
        col: float(shap_vals[i] * predicted_sec / log_abs)
        for i, col in enumerate(feature_cols)
    }

    # Top-K by absolute SHAP impact
    sorted_features = sorted(
        shap_seconds.items(), key=lambda kv: abs(kv[1]), reverse=True
    )[:top_k]

    top_contributors = [
        {
            "feature":        col,
            "label":          FEATURE_LABELS.get(col, col),
            "shap_seconds":   round(val, 1),
            "pct_of_duration": (
                round(val / actual_duration * 100, 1) if actual_duration > 0 else 0.0
            ),
        }
        for col, val in sorted_features
    ]

    return {
        "predicted_expected_seconds": predicted_sec,
        "expected_gap_seconds":       gap_sec,
        "expected_gap_pct":           gap_pct,
        "top_contributors":           top_contributors,
        "shap_by_feature":            {k: round(v, 1) for k, v in shap_seconds.items()},
        "log_prediction":             round(log_pred, 6),
    }

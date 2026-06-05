import numpy as np
import pandas as pd
import mlflow
import mlflow.xgboost
import shap
import tempfile
from pathlib import Path

from sklearn.model_selection import GroupKFold, GridSearchCV
from sklearn.metrics import mean_squared_error, r2_score, make_scorer
from xgboost import XGBRegressor
from azureml.core import Workspace
from mlflow.exceptions import MlflowException

ws = Workspace.from_config()
mlflow.set_tracking_uri(ws.get_mlflow_tracking_uri())
mlflow.set_experiment("pipeline-post-run-analysis")
print(f"Connected: {ws.name}")

X_train  = pd.read_csv("X_train.csv")
X_test   = pd.read_csv("X_test.csv")
y_train  = pd.read_csv("y_train.csv").squeeze()   # log-scale
y_test   = pd.read_csv("y_test.csv").squeeze()
yr_test  = pd.read_csv("yr_test.csv").squeeze()   # raw seconds
train_groups = pd.read_csv("pipeline_train_groups.csv").squeeze()

# Align indexes defensively.
X_train = X_train.reset_index(drop=True)
X_test  = X_test.reset_index(drop=True)
train_groups = train_groups.reset_index(drop=True)

param_grid = {
    "max_depth"       : [4, 6, 8],
    "n_estimators"    : [200, 300, 400],
    "learning_rate"   : [0.01, 0.05, 0.1],
    "subsample"       : [0.7, 0.9],
    "colsample_bytree": [0.7, 0.9],
}

rmse_log_scorer = make_scorer(
    lambda y_true, y_pred: np.sqrt(mean_squared_error(y_true, y_pred)),
    greater_is_better=False
)

cv = GroupKFold(n_splits=5)

grid = GridSearchCV(
    XGBRegressor(random_state=42, n_jobs=1),    # n_jobs=1 inside estimator
    param_grid,
    scoring  = rmse_log_scorer,
    cv       = cv,
    verbose  = 2,
    n_jobs   = -1,                              # parallel folds
    refit    = True,
)

print("\nRunning grouped grid search …")
grid.fit(X_train, y_train, groups=train_groups)

best = grid.best_estimator_
preds_log = best.predict(X_test)
preds_raw = np.expm1(preds_log)

rmse_sec = float(np.sqrt(mean_squared_error(yr_test, preds_raw)))
r2_log   = float(r2_score(y_test, preds_log))
cv_rmse  = float(-grid.best_score_)

print(f"\nBest params : {grid.best_params_}")
print(f"CV RMSE (log scale)  : {cv_rmse:.4f}")
print(f"Hold-out RMSE        : {rmse_sec:.0f}s")
print(f"Hold-out R² (log)    : {r2_log:.4f}")


def safe_log_xgb_model(model_obj):
    """Log xgboost model to MLflow with fallback for backends lacking logged-models API."""
    try:
        mlflow.xgboost.log_model(model_obj, artifact_path="model")
    except MlflowException as ex:
        if "logged-models" not in str(ex):
            raise
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "model"
            mlflow.xgboost.save_model(model_obj, path=str(out_dir))
            mlflow.log_artifacts(str(out_dir), artifact_path="model")

with mlflow.start_run(run_name="xgboost-tuned-best"):
    mlflow.log_params(grid.best_params_)
    mlflow.log_param("model_type",   "XGBoost-tuned")
    mlflow.log_param("target",       "log1p(total_duration_seconds)")
    mlflow.log_param("purpose",      "post-run explanation via SHAP")
    mlflow.log_param("cv_strategy",  "GroupKFold-5 on PipelineName")
    mlflow.log_metric("cv_rmse_log", cv_rmse)
    mlflow.log_metric("rmse_seconds", rmse_sec)
    mlflow.log_metric("r2_log_scale", r2_log)
    safe_log_xgb_model(best)

best.save_model("xgb_best_model.ubj")
print("\nSaved xgb_best_model.ubj  (XGBoost native format — used by register_model.py)")

# ── SHAP feature importance ────────────────────────────────────────────────────
print("\nComputing SHAP values …")
explainer   = shap.TreeExplainer(best)
shap_values = explainer.shap_values(X_train)  # shape: (n_samples, n_features)

mean_abs_shap = np.abs(shap_values).mean(axis=0)
importance_df = (
    pd.DataFrame({"feature": X_train.columns, "mean_abs_shap": mean_abs_shap})
    .sort_values("mean_abs_shap", ascending=False)
    .reset_index(drop=True)
)
importance_df.to_csv("shap_feature_importance.csv", index=False)
print(f"Saved shap_feature_importance.csv  ({len(importance_df)} features)")
print(importance_df.head(10).to_string(index=False))

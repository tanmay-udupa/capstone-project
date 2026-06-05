import pandas as pd
from xgboost import XGBRegressor
from azureml.core import Workspace, Model

# ── Connect ────────────────────────────────────────────────────────────────────
ws = Workspace.from_config()
print(f"Connected: {ws.name}")

# ── Load artefacts ─────────────────────────────────────────────────────────────
model = XGBRegressor()
model.load_model("xgb_best_model.ubj")

importance_df = pd.read_csv("shap_feature_importance.csv")
top_feature   = str(importance_df.iloc[0]["feature"])
top_shap      = float(importance_df.iloc[0]["mean_abs_shap"])

# ── Read actual training row count from saved split ───────────────────────────
X_train = pd.read_csv("X_train.csv")
training_rows = len(X_train)

# ── Register ───────────────────────────────────────────────────────────────────
registered = Model.register(
    workspace   = ws,
    model_path  = "xgb_best_model.ubj",
    model_name  = "pipeline-advisor-xgb",
    description = (
        "XGBoost post-run explanation model for CI/CD pipeline duration. "
        "Used with SHAP to identify bottlenecks and generate per-run "
        "optimization recommendations."
    ),
    tags = {
        "framework"      : "xgboost",
        "task"           : "post-run-explanation",
        "target"         : "log1p(total_duration_seconds)",
        "top_shap_feature": top_feature,
        "top_shap_value" : f"{top_shap:.4f}",
        "training_rows"  : str(training_rows),
        "split_strategy" : "GroupShuffleSplit-PipelineName",
    }
)

print(f"\nRegistered: {registered.name}  version={registered.version}")
print(f"Model ID   : {registered.id}")
print(f"Top feature: {top_feature}  (mean |SHAP| = {top_shap:.4f})")

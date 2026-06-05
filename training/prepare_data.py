import numpy as np
import pandas as pd
from sklearn.model_selection import GroupShuffleSplit

df = pd.read_csv("pipeline_features.csv")
print(f"Loaded: {len(df)} rows, {len(df.columns)} columns")

df["has_test_results"] = (df["total_tests"].fillna(0) > 0).astype(int)

df["test_pass_rate"] = df["test_pass_rate"].fillna(1.0)

# Capture pipeline group labels before dropping identifier columns
pipeline_groups = df["PipelineName"].reset_index(drop=True)

DROP_COLS = ["RunId", "ProjectName", "PipelineName"]
df = df.drop(columns=[c for c in DROP_COLS if c in df.columns])

TARGET_RAW = "total_duration_seconds"
TARGET_LOG = "log_duration_seconds" 

df[TARGET_LOG] = np.log1p(df[TARGET_RAW])

FEATURES = [c for c in df.columns if c not in (TARGET_RAW, TARGET_LOG)]
print(f"\nFeatures ({len(FEATURES)}):\n  {FEATURES}")
print(f"\nTarget (raw): {TARGET_RAW}   Target (log): {TARGET_LOG}")

X = df[FEATURES]
y_log = df[TARGET_LOG]
y_raw = df[TARGET_RAW]

gss = GroupShuffleSplit(n_splits=1, test_size=0.20, random_state=42)
train_idx, test_idx = next(gss.split(X, y_log, groups=pipeline_groups))

X_train, X_test   = X.iloc[train_idx], X.iloc[test_idx]
y_train, y_test   = y_log.iloc[train_idx], y_log.iloc[test_idx]
yr_train, yr_test = y_raw.iloc[train_idx], y_raw.iloc[test_idx]
pipeline_train    = pipeline_groups.iloc[train_idx]
pipeline_test     = pipeline_groups.iloc[test_idx]

print(f"\nTrain: {len(X_train)} rows  |  Test: {len(X_test)} rows")
print(f"Test pipelines: {pipeline_groups.iloc[test_idx].nunique()} unique families "
      f"out of {pipeline_groups.nunique()} total")

X_train.to_csv("X_train.csv",      index=False)
X_test.to_csv("X_test.csv",        index=False)
y_train.to_csv("y_train.csv",      index=False, header=True)
y_test.to_csv("y_test.csv",        index=False, header=True)
yr_train.to_csv("yr_train.csv",    index=False, header=True)
yr_test.to_csv("yr_test.csv",      index=False, header=True)
pipeline_train.to_csv("pipeline_train_groups.csv", index=False, header=True)
pipeline_test.to_csv("pipeline_test_groups.csv",   index=False, header=True)

print("\nSaved: X_train/test.csv, y_train/test.csv (log-scale), yr_train/test.csv (raw seconds), pipeline group files")

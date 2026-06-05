# validate_features.py
import pandas as pd
import matplotlib.pyplot as plt

df = pd.read_csv("pipeline_features.csv")

# 1. Check for nulls
print("=== Null counts ===")
print(df.isnull().sum()[df.isnull().sum() > 0])

# 2. Check for negative durations (data anomalies)
duration_cols = [c for c in df.columns if 'seconds' in c]
for col in duration_cols:
    neg = (df[col] < 0).sum()
    if neg:
        print(f"WARNING: {col} has {neg} negative values")

# 3. Percentile distribution of the target
print("\n=== total_duration_seconds percentiles ===")
print(df['total_duration_seconds'].quantile([0.5, 0.8, 0.9, 0.95, 0.99]))

# 4. Correlation of features vs target
corr = df[duration_cols].corrwith(df['total_duration_seconds']).sort_values(ascending=False)
print("\n=== Feature correlation with total_duration_seconds ===")
print(corr)

# 5. Flag low-signal features
print("\n=== Low-signal features (|correlation| < 0.05) ===")
low_signal = corr[corr.abs() < 0.05].index.tolist()
print(low_signal)

# 6. Check parallelism_ratio distribution
if 'parallelism_ratio' in df.columns:
    print("\n=== parallelism_ratio percentiles ===")
    print(df['parallelism_ratio'].quantile([0.1, 0.25, 0.5, 0.75, 0.9]))
    parallel_pct = (df['parallelism_ratio'] > 1.0).mean() * 100
    print(f"{parallel_pct:.1f}% of runs have parallel execution (ratio > 1.0)")

# 7. Breakdown by pipeline result
print("\n=== Avg total_duration_seconds by result ===")
print(df.groupby(['result_failed', 'result_partial'])['total_duration_seconds'].mean())

# 8. Security scan overhead
if 'security_scan_seconds' in df.columns:
    scan_pct = df['security_scan_seconds'] / df['total_duration_seconds'].replace(0, 1) * 100
    print(f"\n=== Security scan as % of total duration ===")
    print(scan_pct.describe())

# 9. Bar chart of average time buckets
buckets = ['dependency_restore_seconds', 'download_seconds',
           'compile_seconds', 'restore_task_seconds',
           'build_task_seconds', 'test_task_seconds']
df[buckets].mean().sort_values().plot(kind='barh', figsize=(8, 4),
                                       title='Avg seconds by pipeline phase')
plt.tight_layout()
plt.savefig("phase_breakdown.png")
print("Saved phase_breakdown.png")
// API response models matching backend schemas

export interface AnalyzeRequest {
  org: string;
  project: string;
  pipeline_id: number;
  run_id: number;
  top_k_recommendations?: number;
  min_opportunity_seconds?: number;
}

export interface AnalyzeResponse {
  analysis_id: number;
  status: AnalysisStatus;
  message: string;
  poll_url: string;
}

export type AnalysisStatus = 'pending' | 'running' | 'complete' | 'failed';

export interface RunMetrics {
  actual_duration_seconds: number;
  predicted_expected_seconds: number;
  expected_gap_seconds: number;
  expected_gap_pct: number;
}

export interface BenchmarkResult {
  policy_used: string;
  scope_used: string;
  sample_size: number;
  data_sufficiency: string;
  fallback_used: 'none' | 'benchmark_disabled';
  observed_agent_seconds: number;
  typical_agent_seconds: number;
}

export interface TopContributor {
  feature: string;
  label: string;
  feature_value_seconds: number;
  shap_impact_seconds: number;
}

export interface DiagnosisResult {
  top_contributors: TopContributor[];
}

export interface PhaseComparison {
  phase: string;
  observed_seconds: number;
  typical_seconds: number;
  p90_seconds: number;
  above_typical_seconds: number;
}

export interface Recommendation {
  id: string;
  title: string;
  description: string;
  reason_codes: string[];
  figure_seconds: number | null;
  figure_label: string | null;
  share_of_agent_time_pct: number | null;
  observed_seconds: number | null;
  typical_seconds: number | null;
}

export interface DecisionSummary {
  message: string;
}

export interface Versions {
  api_version: string;
  model_version: string;
  feature_schema_version: string;
  benchmark_policy_version: string;
  recommendation_rules_version: string;
}

export interface AnalysisInput {
  org: string;
  project: string;
  pipeline_id: number;
  run_id: number;
}

export interface AnalysisResult {
  analysis_id: number;
  status: AnalysisStatus;
  requested_at_utc?: string;
  completed_at_utc?: string;
  error_message?: string;
  input?: AnalysisInput;
  run_metrics?: RunMetrics;
  benchmark?: BenchmarkResult;
  diagnosis?: DiagnosisResult;
  phase_comparison?: PhaseComparison[];
  recommendations?: Recommendation[];
  decision_summary?: DecisionSummary;
  versions?: Versions;
}

export interface HealthResponse {
  status: string;
  model_loaded: boolean;
  db_reachable: boolean;
}

// ADO browsing models
export interface AdoOrganization {
  id: string;
  name: string;
  url: string;
}

export interface AdoProject {
  id: string;
  name: string;
  state: string;
  description?: string;
}

export interface AdoPipeline {
  id: number;
  name: string;
  folder?: string;
}

export interface AdoRun {
  id: number;
  name: string;
  state: string;
  result: string | null;
  created_date: string | null;
  finished_date: string | null;
  duration_seconds: number | null;
  branch: string | null;
}

export interface AdoOrganizationsResponse {
  organizations: AdoOrganization[];
}

export interface AdoProjectsResponse {
  org: string;
  projects: AdoProject[];
}

export interface AdoPipelinesResponse {
  org: string;
  project: string;
  pipelines: AdoPipeline[];
}

export interface AdoRunsResponse {
  org: string;
  project: string;
  pipeline_id: number;
  runs: AdoRun[];
}

// Insights (Pipeline Efficiency page)
export interface InsightsScope {
  project: string | null;
  days: number | null;
  runs: number;
  pipelines: number;
  first_run_utc: string | null;
  last_run_utc: string | null;
  span_days: number;
  agent_seconds: number;
  incomplete_before_utc: string | null;
  agent_seconds_per_30_days: number | null;
}

export interface ComputeBreakdown {
  name: string;
  runs: number;
  agent_seconds: number;
}

export interface RelatedRun {
  run_id: number;
  result: string;
  url: string | null;
}

export interface ExampleRun {
  run_id: number;
  pipeline: string;
  started_utc: string | null;
  agent_seconds: number;
  url: string | null;
  related: RelatedRun | null;
}

export interface FailedTaskCount {
  pipeline: string;
  task: string;
  runs: number;
}

export interface WasteCategory {
  key: string;
  title: string;
  runs: number;
  agent_seconds: number;
  likely_avoidable: boolean;
  fix: string;
  pipelines: ComputeBreakdown[];
  examples: ExampleRun[];
  related_label: string | null;
  failed_tasks: FailedTaskCount[];
}

export interface PipelineCompute {
  pipeline: string;
  runs: number;
  agent_seconds: number;
  failed_or_canceled_seconds: number;
}

export interface TaskCompute {
  pipeline: string;
  task: string;
  runs: number;
  occurrences: number;
  agent_seconds: number;
}

export interface RepeatedTask {
  pipeline: string;
  task: string;
  runs: number;
  extra_occurrences: number;
  repeated_seconds: number;
}

export interface ProjectOption {
  name: string;
  runs: number;
}

export interface InsightsProjectsResponse {
  projects: ProjectOption[];
}

export interface InsightsResponse {
  scope: InsightsScope;
  by_outcome: ComputeBreakdown[];
  by_pool: ComputeBreakdown[];
  waste: WasteCategory[];
  top_pipelines: PipelineCompute[];
  top_tasks: TaskCompute[];
  repeated_tasks: RepeatedTask[];
}

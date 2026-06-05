// API response models matching backend schemas

export interface AnalyzeRequest {
  org: string;
  project: string;
  pipeline_id: number;
  run_id: number;
  benchmark_policy?: string;
  comparison_scope?: string;
  top_k_recommendations?: number;
  min_confidence?: number;
  min_opportunity_seconds?: number;
  min_shap_impact_seconds?: number;
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
  target_total_seconds: number;
  total_opportunity_seconds: number;
  total_opportunity_pct: number;
  sample_size: number;
  data_sufficiency: string;
  fallback_used: string;
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

export interface OpportunityByPhase {
  phase: string;
  observed_seconds: number;
  benchmark_seconds: number;
  opportunity_seconds: number;
}

export interface Recommendation {
  id: string;
  title: string;
  description: string;
  reason_codes: string[];
  priority: 'high' | 'medium' | 'low';
  confidence: number;
  estimated_savings_seconds: number;
  estimated_savings_pct: number;
  estimated_savings_pct_of_run: number;
  estimated_savings_pct_of_phase: number;
}

export interface DecisionSummary {
  actionability: 'high' | 'medium' | 'low';
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
  opportunity_by_phase?: OpportunityByPhase[];
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

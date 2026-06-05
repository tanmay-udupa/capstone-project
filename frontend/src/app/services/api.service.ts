import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { environment } from '../../environments/environment';
import {
  AnalyzeRequest,
  AnalyzeResponse,
  AnalysisResult,
  HealthResponse,
  AdoOrganizationsResponse,
  AdoProjectsResponse,
  AdoPipelinesResponse,
  AdoRunsResponse,
} from '../models';

@Injectable({ providedIn: 'root' })
export class ApiService {
  private http = inject(HttpClient);
  private baseUrl = environment.apiBaseUrl;

  // Health
  getHealth(): Observable<HealthResponse> {
    return this.http.get<HealthResponse>(`${this.baseUrl}/v1/health`);
  }

  // Analysis
  createAnalysis(req: AnalyzeRequest): Observable<AnalyzeResponse> {
    return this.http.post<AnalyzeResponse>(`${this.baseUrl}/v1/analyses`, req);
  }

  getAnalysis(analysisId: number): Observable<AnalyzeResponse> {
    return this.http.get<AnalyzeResponse>(`${this.baseUrl}/v1/analyses/${analysisId}`);
  }

  getAnalysisResult(analysisId: number): Observable<AnalysisResult> {
    return this.http.get<AnalysisResult>(`${this.baseUrl}/v1/analyses/${analysisId}`);
  }

  getRecommendations(analysisId: number): Observable<any> {
    return this.http.get(`${this.baseUrl}/v1/analyses/${analysisId}/recommendations`);
  }

  // ADO Browsing
  getOrganizations(): Observable<AdoOrganizationsResponse> {
    return this.http.get<AdoOrganizationsResponse>(`${this.baseUrl}/v1/ado/organizations`);
  }

  getProjects(org: string): Observable<AdoProjectsResponse> {
    return this.http.get<AdoProjectsResponse>(`${this.baseUrl}/v1/ado/${org}/projects`);
  }

  getPipelines(org: string, project: string): Observable<AdoPipelinesResponse> {
    return this.http.get<AdoPipelinesResponse>(
      `${this.baseUrl}/v1/ado/${org}/${project}/pipelines`
    );
  }

  getRuns(org: string, project: string, pipelineId: number, top = 50): Observable<AdoRunsResponse> {
    return this.http.get<AdoRunsResponse>(
      `${this.baseUrl}/v1/ado/${org}/${project}/pipelines/${pipelineId}/runs`,
      { params: { top: top.toString() } }
    );
  }
}

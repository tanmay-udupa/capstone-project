import { Component, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatSelectModule } from '@angular/material/select';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTableModule } from '@angular/material/table';
import { MatChipsModule } from '@angular/material/chips';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatDividerModule } from '@angular/material/divider';
import { ApiService } from '../../services/api.service';
import {
  AdoOrganization,
  AdoProject,
  AdoPipeline,
  AdoRun,
} from '../../models';

@Component({
  selector: 'app-analyze',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatCardModule,
    MatButtonModule,
    MatIconModule,
    MatSelectModule,
    MatFormFieldModule,
    MatProgressSpinnerModule,
    MatTableModule,
    MatChipsModule,
    MatSnackBarModule,
    MatTooltipModule,
    MatDividerModule,
  ],
  templateUrl: './analyze.component.html',
  styleUrl: './analyze.component.scss',
})
export class AnalyzeComponent implements OnInit {
  private api = inject(ApiService);
  private router = inject(Router);
  private snackBar = inject(MatSnackBar);

  // Selection state
  organizations: AdoOrganization[] = [];
  projects: AdoProject[] = [];
  pipelines: AdoPipeline[] = [];
  runs: AdoRun[] = [];

  selectedOrg: string = '';
  selectedProject: string = '';
  selectedPipeline: number | null = null;

  // Loading states
  loadingOrgs = false;
  loadingProjects = false;
  loadingPipelines = false;
  loadingRuns = false;
  analyzingRunId: number | null = null;

  // Table columns
  displayedColumns = ['id', 'name', 'state', 'result', 'duration', 'created', 'actions'];

  ngOnInit(): void {
    this.loadOrganizations();
  }

  loadOrganizations(): void {
    this.loadingOrgs = true;
    this.api.getOrganizations().subscribe({
      next: (res) => {
        this.organizations = res.organizations;
        this.loadingOrgs = false;
      },
      error: () => {
        this.snackBar.open('Failed to load organizations', 'Dismiss', { duration: 5000 });
        this.loadingOrgs = false;
      },
    });
  }

  onOrgChange(): void {
    this.projects = [];
    this.pipelines = [];
    this.runs = [];
    this.selectedProject = '';
    this.selectedPipeline = null;

    if (!this.selectedOrg) return;

    this.loadingProjects = true;
    this.api.getProjects(this.selectedOrg).subscribe({
      next: (res) => {
        this.projects = res.projects;
        this.loadingProjects = false;
      },
      error: () => {
        this.snackBar.open('Failed to load projects', 'Dismiss', { duration: 5000 });
        this.loadingProjects = false;
      },
    });
  }

  onProjectChange(): void {
    this.pipelines = [];
    this.runs = [];
    this.selectedPipeline = null;

    if (!this.selectedProject) return;

    this.loadingPipelines = true;
    this.api.getPipelines(this.selectedOrg, this.selectedProject).subscribe({
      next: (res) => {
        this.pipelines = res.pipelines;
        this.loadingPipelines = false;
      },
      error: () => {
        this.snackBar.open('Failed to load pipelines', 'Dismiss', { duration: 5000 });
        this.loadingPipelines = false;
      },
    });
  }

  onPipelineChange(): void {
    this.runs = [];
    if (!this.selectedPipeline) return;

    this.loadingRuns = true;
    this.api.getRuns(this.selectedOrg, this.selectedProject, this.selectedPipeline).subscribe({
      next: (res) => {
        this.runs = res.runs;
        this.loadingRuns = false;
      },
      error: () => {
        this.snackBar.open('Failed to load runs', 'Dismiss', { duration: 5000 });
        this.loadingRuns = false;
      },
    });
  }

  analyzeRun(run: AdoRun): void {
    if (!this.selectedPipeline) return;

    this.analyzingRunId = run.id;
    this.api
      .createAnalysis({
        org: this.selectedOrg,
        project: this.selectedProject,
        pipeline_id: this.selectedPipeline,
        run_id: run.id,
      })
      .subscribe({
        next: (res) => {
          this.analyzingRunId = null;
          this.router.navigate(['/results', res.analysis_id]);
        },
        error: (err) => {
          this.analyzingRunId = null;
          this.snackBar.open(
            err?.error?.detail || 'Analysis request failed',
            'Dismiss',
            { duration: 5000 }
          );
        },
      });
  }

  formatDuration(seconds: number | null): string {
    if (seconds === null) return '—';
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return m > 0 ? `${m}m ${s}s` : `${s}s`;
  }

  getResultClass(result: string | null): string {
    switch (result) {
      case 'succeeded': return 'success';
      case 'failed': return 'error';
      case 'canceled': return 'warning';
      case 'partiallySucceeded': return 'warning';
      default: return 'info';
    }
  }
}

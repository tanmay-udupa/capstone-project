import { Component, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatSelectModule } from '@angular/material/select';
import { ApiService } from '../../services/api.service';
import { InsightsResponse, ProjectOption } from '../../models';

interface ComputeTotal {
  runs: number;
  agent_seconds: number;
}

const OUTCOME_LABELS: Record<string, string> = {
  succeeded: 'Succeeded',
  partiallySucceeded: 'Partially succeeded',
  failed: 'Failed',
  canceled: 'Canceled',
};

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    MatCardModule,
    MatButtonModule,
    MatIconModule,
    MatProgressSpinnerModule,
    MatFormFieldModule,
    MatSelectModule,
  ],
  templateUrl: './dashboard.component.html',
  styleUrl: './dashboard.component.scss',
})
export class DashboardComponent implements OnInit {
  private api = inject(ApiService);
  private router = inject(Router);

  // 0 means no window: mat-select treats a null option value as clearing the selection.
  readonly periods = [
    { label: 'All available data', days: 0 },
    { label: 'Latest 30 days', days: 30 },
    { label: 'Latest 90 days', days: 90 },
    { label: 'Latest 180 days', days: 180 },
  ];

  insights: InsightsResponse | null = null;
  projects: ProjectOption[] = [];
  // null until the user picks; '' means all projects.
  selectedProject: string | null = null;
  selectedDays: number | null = null;
  loadingProjects = false;
  loading = false;
  error: string | null = null;
  expandedCategory: string | null = null;

  ngOnInit(): void {
    this.loadProjects();
  }

  get selectionComplete(): boolean {
    return this.selectedProject !== null && this.selectedDays !== null;
  }

  loadProjects(): void {
    this.loadingProjects = true;
    this.error = null;
    this.api.getInsightProjects().subscribe({
      next: (res) => {
        this.projects = res.projects;
        this.loadingProjects = false;
      },
      error: () => {
        this.error = 'Unable to load projects from the backend API.';
        this.loadingProjects = false;
      },
    });
  }

  onSelectionChange(): void {
    if (this.selectionComplete) this.loadInsights();
  }

  retry(): void {
    if (this.selectionComplete) {
      this.loadInsights();
    } else {
      this.loadProjects();
    }
  }

  loadInsights(): void {
    this.loading = true;
    this.error = null;
    this.api.getInsights(this.selectedProject || undefined, this.selectedDays || undefined).subscribe({
      next: (res) => {
        this.insights = res;
        this.loading = false;
      },
      error: () => {
        this.error = 'Unable to load pipeline efficiency data from the backend API.';
        this.loading = false;
      },
    });
  }

  navigateToAnalyze(): void {
    this.router.navigate(['/analyze']);
  }

  toggleCategory(key: string): void {
    this.expandedCategory = this.expandedCategory === key ? null : key;
  }

  get per30DaysHours(): number | null {
    const seconds = this.insights?.scope.agent_seconds_per_30_days ?? null;
    return seconds === null ? null : this.hours(seconds);
  }

  get unsuccessfulSeconds(): number {
    return (this.insights?.by_outcome ?? [])
      .filter((o) => o.name === 'failed' || o.name === 'canceled')
      .reduce((sum, o) => sum + o.agent_seconds, 0);
  }

  get scheduledRebuilds(): ComputeTotal {
    return this.sumWaste('scheduled_rebuild_passed', 'scheduled_rebuild_failed');
  }

  get repeatedWork(): ComputeTotal {
    return this.sumWaste('repeated_tasks');
  }

  hours(seconds: number): number {
    return seconds / 3600;
  }

  share(seconds: number): number {
    return this.ratio(seconds, this.insights?.scope.agent_seconds ?? 0);
  }

  ratio(part: number, whole: number): number {
    return whole > 0 ? (part / whole) * 100 : 0;
  }

  minutesPerRun(seconds: number, runs: number): number {
    return runs > 0 ? seconds / runs / 60 : 0;
  }

  outcomeLabel(name: string): string {
    return OUTCOME_LABELS[name] ?? name;
  }

  resultClass(result: string): string {
    switch (result) {
      case 'succeeded': return 'success';
      case 'failed': return 'error';
      case 'partiallySucceeded':
      case 'canceled': return 'warning';
      default: return 'info';
    }
  }

  outcomeClass(name: string): string {
    switch (name) {
      case 'succeeded': return 'success';
      case 'failed': return 'error';
      case 'canceled': return 'warning';
      case 'partiallySucceeded': return '';
      default: return 'muted';
    }
  }

  poolLabel(name: string): string {
    return name === 'Azure Pipelines' ? `${name} (Microsoft-hosted)` : name;
  }

  private sumWaste(...keys: string[]): ComputeTotal {
    return (this.insights?.waste ?? [])
      .filter((w) => keys.includes(w.key))
      .reduce(
        (total, w) => ({ runs: total.runs + w.runs, agent_seconds: total.agent_seconds + w.agent_seconds }),
        { runs: 0, agent_seconds: 0 },
      );
  }
}

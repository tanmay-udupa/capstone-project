import { Component, OnInit, OnDestroy, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { MatChipsModule } from '@angular/material/chips';
import { MatDividerModule } from '@angular/material/divider';
import { MatTabsModule } from '@angular/material/tabs';
import { MatExpansionModule } from '@angular/material/expansion';
import { ApiService } from '../../services/api.service';
import { AnalysisResult, AnalysisStatus } from '../../models';

const PHASE_LABELS: Record<string, string> = {
  restore: 'Package restore',
  download: 'Checkout and download',
  build: 'Build / compile',
  test: 'Tests',
  deploy: 'Deploy / publish',
  security_scan: 'Security scans',
  firewall: 'Firewall rules',
};

@Component({
  selector: 'app-results',
  standalone: true,
  imports: [
    CommonModule,
    MatCardModule,
    MatButtonModule,
    MatIconModule,
    MatProgressSpinnerModule,
    MatProgressBarModule,
    MatChipsModule,
    MatDividerModule,
    MatTabsModule,
    MatExpansionModule,
  ],
  templateUrl: './results.component.html',
  styleUrl: './results.component.scss',
})
export class ResultsComponent implements OnInit, OnDestroy {
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  private api = inject(ApiService);

  analysisId!: number;
  result: AnalysisResult | null = null;
  status: AnalysisStatus = 'pending';
  loading = true;
  error: string | null = null;

  private pollTimer: any = null;

  ngOnInit(): void {
    this.analysisId = Number(this.route.snapshot.paramMap.get('id'));
    this.pollStatus();
  }

  ngOnDestroy(): void {
    if (this.pollTimer) {
      clearTimeout(this.pollTimer);
    }
  }

  pollStatus(): void {
    this.api.getAnalysis(this.analysisId).subscribe({
      next: (res) => {
        this.status = res.status;
        if (res.status === 'complete') {
          this.loadFullResult();
        } else if (res.status === 'failed') {
          this.loading = false;
          this.error = res.message || 'Analysis failed.';
        } else {
          this.pollTimer = setTimeout(() => this.pollStatus(), 2000);
        }
      },
      error: (err) => {
        this.loading = false;
        this.error = 'Failed to fetch analysis status.';
      },
    });
  }

  loadFullResult(): void {
    this.api.getAnalysisResult(this.analysisId).subscribe({
      next: (res) => {
        this.result = res;
        this.loading = false;
      },
      error: (err) => {
        this.loading = false;
        this.error = err?.error?.detail || 'Failed to load analysis results.';
      },
    });
  }

  get hasBaseline(): boolean {
    return this.result?.benchmark?.fallback_used === 'none';
  }

  get agentTimeDifference(): number {
    const benchmark = this.result?.benchmark;
    return benchmark ? benchmark.observed_agent_seconds - benchmark.typical_agent_seconds : 0;
  }

  formatMinutes(seconds: number | null | undefined): string {
    const totalMinutes = Math.round(Math.abs(seconds ?? 0) / 60);
    if (totalMinutes < 60) return `${totalMinutes} min`;
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    return minutes ? `${hours} h ${minutes} min` : `${hours} h`;
  }

  phaseLabel(phase: string): string {
    return PHASE_LABELS[phase] ?? phase;
  }

  goBack(): void {
    this.router.navigate(['/analyze']);
  }

  goToDashboard(): void {
    this.router.navigate(['/dashboard']);
  }
}

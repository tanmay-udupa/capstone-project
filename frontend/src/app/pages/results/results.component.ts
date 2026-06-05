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
    this.api.getRecommendations(this.analysisId).subscribe({
      next: (res) => {
        this.result = {
          analysis_id: this.analysisId,
          status: 'complete',
          recommendations: res.recommendations,
          decision_summary: res.decision_summary,
        } as AnalysisResult;
        this.loading = false;
      },
      error: () => {
        this.loading = false;
        this.error = 'Failed to load analysis results.';
      },
    });
  }

  formatDuration(seconds: number): string {
    if (!seconds) return '0s';
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
  }

  getPriorityClass(priority: string): string {
    switch (priority) {
      case 'high': return 'error';
      case 'medium': return 'warning';
      case 'low': return 'info';
      default: return 'info';
    }
  }

  getActionabilityClass(actionability: string): string {
    switch (actionability) {
      case 'high': return 'success';
      case 'medium': return 'warning';
      case 'low': return 'info';
      default: return 'info';
    }
  }

  getConfidencePercent(confidence: number): number {
    return Math.round(confidence * 100);
  }

  goBack(): void {
    this.router.navigate(['/analyze']);
  }

  goToDashboard(): void {
    this.router.navigate(['/dashboard']);
  }
}

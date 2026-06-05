import { Component, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { Router } from '@angular/router';
import { MatCardModule } from '@angular/material/card';
import { MatButtonModule } from '@angular/material/button';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatChipsModule } from '@angular/material/chips';
import { ApiService } from '../../services/api.service';
import { HealthResponse } from '../../models';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [
    CommonModule,
    MatCardModule,
    MatButtonModule,
    MatIconModule,
    MatProgressSpinnerModule,
    MatChipsModule,
  ],
  templateUrl: './dashboard.component.html',
  styleUrl: './dashboard.component.scss',
})
export class DashboardComponent implements OnInit {
  private api = inject(ApiService);
  private router = inject(Router);

  health: HealthResponse | null = null;
  loading = true;
  error: string | null = null;

  ngOnInit(): void {
    this.loadHealth();
  }

  loadHealth(): void {
    this.loading = true;
    this.api.getHealth().subscribe({
      next: (h) => {
        this.health = h;
        this.loading = false;
      },
      error: (err) => {
        this.error = 'Unable to connect to backend API.';
        this.loading = false;
      },
    });
  }

  navigateToAnalyze(): void {
    this.router.navigate(['/analyze']);
  }

  getStatusClass(status: string): string {
    return status === 'ready' ? 'success' : 'warning';
  }
}

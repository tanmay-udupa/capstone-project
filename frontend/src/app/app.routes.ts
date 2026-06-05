import { Routes } from '@angular/router';
import { MsalGuard } from '@azure/msal-angular';

export const routes: Routes = [
  {
    path: '',
    redirectTo: 'dashboard',
    pathMatch: 'full',
  },
  {
    path: 'dashboard',
    loadComponent: () =>
      import('./pages/dashboard/dashboard.component').then(m => m.DashboardComponent),
    canActivate: [MsalGuard],
  },
  {
    path: 'analyze',
    loadComponent: () =>
      import('./pages/analyze/analyze.component').then(m => m.AnalyzeComponent),
    canActivate: [MsalGuard],
  },
  {
    path: 'results/:id',
    loadComponent: () =>
      import('./pages/results/results.component').then(m => m.ResultsComponent),
    canActivate: [MsalGuard],
  },
  {
    path: 'login',
    loadComponent: () =>
      import('./pages/login/login.component').then(m => m.LoginComponent),
  },
  {
    path: '**',
    redirectTo: 'dashboard',
  },
];

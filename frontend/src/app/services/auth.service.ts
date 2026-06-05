import { Injectable, inject, OnDestroy } from '@angular/core';
import { MsalBroadcastService, MsalService } from '@azure/msal-angular';
import { InteractionStatus, AccountInfo } from '@azure/msal-browser';
import { BehaviorSubject, Subject, filter, takeUntil } from 'rxjs';

@Injectable({ providedIn: 'root' })
export class AuthService implements OnDestroy {
  private msalService = inject(MsalService);
  private msalBroadcast = inject(MsalBroadcastService);
  private destroy$ = new Subject<void>();

  private _isAuthenticated$ = new BehaviorSubject<boolean>(false);
  isAuthenticated$ = this._isAuthenticated$.asObservable();

  private _user$ = new BehaviorSubject<AccountInfo | null>(null);
  user$ = this._user$.asObservable();

  constructor() {
    this.msalBroadcast.inProgress$
      .pipe(
        filter((status) => status === InteractionStatus.None),
        takeUntil(this.destroy$)
      )
      .subscribe(() => {
        this.checkAccount();
      });
  }

  private checkAccount(): void {
    const accounts = this.msalService.instance.getAllAccounts();
    const isAuth = accounts.length > 0;
    this._isAuthenticated$.next(isAuth);
    this._user$.next(isAuth ? accounts[0] : null);

    if (isAuth) {
      this.msalService.instance.setActiveAccount(accounts[0]);
    }
  }

  login(): void {
    this.msalService.loginRedirect();
  }

  logout(): void {
    this.msalService.logoutRedirect();
  }

  getDisplayName(): string {
    const account = this.msalService.instance.getActiveAccount();
    return account?.name || account?.username || 'User';
  }

  getEmail(): string {
    const account = this.msalService.instance.getActiveAccount();
    return account?.username || '';
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }
}

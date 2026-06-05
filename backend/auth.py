from __future__ import annotations

import time
import threading

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from config import settings

bearer_scheme = HTTPBearer()

# ── JWKS cache with TTL ────────────────────────────────────────────────────────
_JWKS_TTL_SECONDS = 3600  # re-fetch keys every hour to handle key rotation
_jwks_cache: dict | None = None
_jwks_fetched_at: float = 0.0
_jwks_lock = threading.Lock()


def _get_jwks() -> dict:
    """Fetch Entra ID JWKS, caching the result for up to 1 hour.

    Uses a lock so concurrent requests don't trigger duplicate fetches.
    Automatically refreshes when keys rotate.
    """
    global _jwks_cache, _jwks_fetched_at
    now = time.monotonic()

    if _jwks_cache is not None and (now - _jwks_fetched_at) < _JWKS_TTL_SECONDS:
        return _jwks_cache

    with _jwks_lock:
        # Re-check inside the lock in case another thread already refreshed.
        if _jwks_cache is not None and (now - _jwks_fetched_at) < _JWKS_TTL_SECONDS:
            return _jwks_cache

        url = (
            f"https://login.microsoftonline.com/"
            f"{settings.TENANT_ID}/discovery/v2.0/keys"
        )
        resp = httpx.get(url, timeout=10)
        resp.raise_for_status()
        _jwks_cache = resp.json()
        _jwks_fetched_at = time.monotonic()
        return _jwks_cache


def validate_token(
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    """
    Validate Bearer token against Entra ID JWKS.

    When DEV_SKIP_AUTH=True (local dev only), skips all JWKS validation and
    returns a synthetic claims dict so the API can be tested without a real
    Entra token. Never set this in production.

    Attaches the raw token to request.state.raw_token so the ADO OBO
    flow in ado_client.py can retrieve it without re-parsing the header.

    Returns the decoded JWT payload (claims dict).
    Raises HTTP 401 on any validation failure.
    """
    if settings.DEV_SKIP_AUTH:
        request.state.raw_token = creds.credentials
        return {
            "sub": "dev-user",
            "preferred_username": "dev@local",
            "aud": settings.CLIENT_ID or "dev",
        }

    token = creds.credentials
    try:
        jwks    = _get_jwks()
        header  = jwt.get_unverified_header(token)
        key     = next(
            (k for k in jwks["keys"] if k["kid"] == header["kid"]),
            None,
        )
        if key is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token signing key not found in JWKS — try again or contact admin.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        payload = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            options={"verify_exp": True, "verify_aud": False},
        )

        # Accept both common audience shapes for Entra-protected custom APIs:
        # 1) bare app client ID GUID
        # 2) api://<client_id>
        aud = payload.get("aud")
        allowed_audiences = {
            settings.CLIENT_ID,
            f"api://{settings.CLIENT_ID}",
        }
        if isinstance(aud, str):
            aud_ok = aud in allowed_audiences
        elif isinstance(aud, list):
            aud_ok = any(a in allowed_audiences for a in aud)
        else:
            aud_ok = False

        if not aud_ok:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=(
                    "Invalid audience. Expected one of: "
                    f"{', '.join(sorted(allowed_audiences))}. "
                    f"Received: {aud}"
                ),
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Preserve raw token for downstream OBO exchange
        request.state.raw_token = token
        return payload

    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )

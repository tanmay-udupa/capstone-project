from __future__ import annotations

import functools

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from config import settings

bearer_scheme = HTTPBearer()


@functools.lru_cache(maxsize=1)
def _get_jwks() -> dict:
    """Fetch and in-process-cache the Entra ID JSON Web Key Set."""
    url = (
        f"https://login.microsoftonline.com/"
        f"{settings.TENANT_ID}/discovery/v2.0/keys"
    )
    resp = httpx.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()


def validate_token(
    request: Request,
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    """
    Validate Bearer token against Entra ID JWKS.

    Attaches the raw token to request.state.raw_token so the ADO OBO
    flow in ado_client.py can retrieve it without re-parsing the header.

    Returns the decoded JWT payload (claims dict).
    Raises HTTP 401 on any validation failure.
    """
    token = creds.credentials
    try:
        jwks    = _get_jwks()
        header  = jwt.get_unverified_header(token)
        key     = next(
            k for k in jwks["keys"] if k["kid"] == header["kid"]
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

    except StopIteration:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token signing key not found in JWKS — try again or contact admin.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )

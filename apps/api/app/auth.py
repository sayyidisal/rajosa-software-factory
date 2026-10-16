"""Bearer-token gate for task routes (Phase 4H.1).

Fail-closed: if ``RAJOSA_API_TOKEN`` is unset/empty, protected routes return
401. Loopback binding is not authentication. Tokens are never logged or
returned. Query-string and body tokens are not accepted.
"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Header, HTTPException, Request, status

# Shared unauthorized detail — do not distinguish missing vs wrong vs unconfigured.
_UNAUTHORIZED_DETAIL = "Authentication required"

# Reject trivially short configured tokens (misconfiguration).
_MIN_TOKEN_LENGTH = 16


def normalize_api_token(raw: str | None) -> str | None:
    """Return a usable token or ``None`` if unset/blank/too short."""
    if raw is None:
        return None
    token = raw.strip()
    if not token:
        return None
    if len(token) < _MIN_TOKEN_LENGTH:
        return None
    return token


def require_api_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Require ``Authorization: Bearer <token>`` matching configured settings."""
    settings = getattr(request.app.state, "settings", None)
    expected = normalize_api_token(
        getattr(settings, "rajosa_api_token", None) if settings is not None else None
    )
    if expected is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_UNAUTHORIZED_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        )

    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_UNAUTHORIZED_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        )

    presented = authorization[len("Bearer ") :]
    # Constant-time compare on UTF-8 bytes; length mismatch → False (no early equal).
    try:
        ok = secrets.compare_digest(
            presented.encode("utf-8"),
            expected.encode("utf-8"),
        )
    except (TypeError, ValueError):
        ok = False
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_UNAUTHORIZED_DETAIL,
            headers={"WWW-Authenticate": "Bearer"},
        )

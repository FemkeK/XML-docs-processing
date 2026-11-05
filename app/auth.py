"""API-key authentication through the `X-API-Key` header.

Deliberately simple: one or more static keys from configuration. See the README for
the step to OAuth2/OIDC with roles (for example, separate rights to submit and to read).
"""

import hmac

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

# auto_error=False so we return our own 401 (with WWW-Authenticate) instead of FastAPI's 403.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(request: Request, api_key: str | None = Security(api_key_header)) -> None:
    """FastAPI dependency: raise 401 unless the request carries a configured API key.

    Applied to the whole /documents router in api.py, so no endpoint can forget it.
    """
    valid_keys: list[str] = request.app.state.settings.api_keys
    # compare_digest takes the same time whether a key matches or not, which prevents
    # timing attacks. The list (not a generator) makes sure every key is compared, so the
    # response time does not reveal which key matched either.
    if not api_key or not any(
        [hmac.compare_digest(api_key.encode(), key.encode()) for key in valid_keys]
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )

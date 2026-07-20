"""
Internal-token auth for admin endpoints.

Why: ``/api/run/{job_name}`` and ``/api/alerts/webhook`` can trigger
arbitrary work or relay messages. They should not be reachable from the
public internet without a check.

The token is read from env ``INTERNAL_API_TOKEN``. When unset, the
module still functions in **open mode** (any caller allowed) — this
keeps the dev experience frictionless. Production deployments should
set the token; the open-mode warning is logged at startup.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Optional

from fastapi import Header, HTTPException, status
from loguru import logger


def _get_token() -> Optional[str]:
    return os.environ.get("INTERNAL_API_TOKEN") or None


def is_protected() -> bool:
    """True when a token has been configured (auth enforced)."""
    return bool(_get_token())


def verify_internal_token(
    x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """FastAPI dependency: enforce the internal token.

    - When ``INTERNAL_API_TOKEN`` is unset: allow all (dev mode) and log
      a one-shot warning.
    - When set: require a matching ``X-Internal-Token`` header.
    """
    expected = _get_token()
    if not expected:
        # Open mode. Log once per process; cheap to check repeatedly.
        if not getattr(verify_internal_token, "_warned", False):
            logger.warning(
                "INTERNAL_API_TOKEN is not set — admin endpoints are open. "
                "Set INTERNAL_API_TOKEN in production."
            )
            verify_internal_token._warned = True
        return

    if not x_internal_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Internal-Token header",
        )
    # Constant-time comparison to avoid timing attacks.
    if not hmac.compare_digest(x_internal_token, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid internal token",
        )

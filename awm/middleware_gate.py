"""Destructive-op gate for the exposed listener.

Routes that delete or create filesystem state are 403'd unless the operator
has explicitly set ``AWM_ALLOW_DESTRUCTIVE=1``. Read on each request so the
operator can flip the gate without restarting.
"""

from __future__ import annotations

import os

from fastapi import HTTPException, status


def _allowed() -> bool:
    return os.environ.get("AWM_ALLOW_DESTRUCTIVE") == "1"


def require_destructive() -> None:
    """FastAPI dependency. 403 if destructive ops aren't enabled."""
    if not _allowed():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="destructive operations disabled (set AWM_ALLOW_DESTRUCTIVE=1)",
        )

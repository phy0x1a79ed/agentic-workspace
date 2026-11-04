"""Task ids (sessions.md R58) and the session ids they group (R9)."""

from __future__ import annotations

import base64
import re
import uuid

from awm.gatewayclient import ids

TASK_ID_LEN = 8
_TASK_ID = re.compile(r"[A-Za-z0-9_-]{8}")


def mint_task_id(raw: bytes | None = None) -> str:
    """The first 8 characters of a random UUID in URL-safe base64.

    redux-queue's minter pins the same vector: bytes 00112233-4455-6677-8899-aabbccddeeff
    give ``ABEiM0RV``.
    """
    return base64.urlsafe_b64encode(raw if raw is not None else uuid.uuid4().bytes).decode()[:TASK_ID_LEN]


def is_task_id(value: object) -> bool:
    return isinstance(value, str) and _TASK_ID.fullmatch(value) is not None


def session_key(value: object) -> str:
    """A session id in R9 form, from the R9 form or a Claude session UUID."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a session id is required")
    key = ids.to_r9(value)
    if key is None:
        raise ValueError(f"{value!r} is not a session id: give the R9 form (cc + hex) or a Claude session UUID")
    return key


def claude_uuid(key: str) -> str | None:
    """The Claude session UUID an R9 Claude id stands for, else None."""
    return ids.claude_uuid(key)

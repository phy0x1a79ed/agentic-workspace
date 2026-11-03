"""Task ids (sessions.md R58) and the session ids they group (R9)."""

from __future__ import annotations

import base64
import re
import uuid

TASK_ID_LEN = 8
_TASK_ID = re.compile(r"[A-Za-z0-9_-]{8}")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_R9 = re.compile(r"[a-z]{2}[0-9a-f]{16,}")
_HEX32 = re.compile(r"[0-9a-fA-F]{32}")
PLATFORM_CLAUDE = "cc"
#: The platforms that mint session ids, so a job id such as `job00001` never reads as one.
PLATFORMS = ("cc", "ds")


def mint_task_id(raw: bytes | None = None) -> str:
    """The first 8 characters of a random UUID in URL-safe base64.

    redux-queue's minter pins the same vector: bytes 00112233-4455-6677-8899-aabbccddeeff
    give ``ABEiM0RV``.
    """
    return base64.urlsafe_b64encode(raw if raw is not None else uuid.uuid4().bytes).decode()[:TASK_ID_LEN]


def is_task_id(value: object) -> bool:
    return isinstance(value, str) and _TASK_ID.fullmatch(value) is not None


def session_key(value: object) -> str:
    """A session id in R9 form, from the R9 form, a Claude session UUID or its 32 hex digits."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a session id is required")
    value = value.strip()
    if _UUID.fullmatch(value) or _HEX32.fullmatch(value):
        return PLATFORM_CLAUDE + value.replace("-", "").lower()
    if _R9.fullmatch(value) and value[:2] in PLATFORMS and (value[:2] != PLATFORM_CLAUDE or len(value) == 34):
        return value
    raise ValueError(f"{value!r} is not a session id: give the R9 form (cc + hex) or a Claude session UUID")


def claude_uuid(key: str) -> str | None:
    """The Claude session UUID an R9 Claude id stands for, else None."""
    if not key.startswith(PLATFORM_CLAUDE) or len(key) != 34:
        return None
    h = key[2:]
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"

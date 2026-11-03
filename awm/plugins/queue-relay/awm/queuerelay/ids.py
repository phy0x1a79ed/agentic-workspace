"""Task ids (sessions.md R58): the first 8 characters of a UUID4 in URL-safe base64."""

from __future__ import annotations

import base64
import re
import uuid

TASK_ID = re.compile(r"^[A-Za-z0-9_-]{8}$")


def mint_task_id(u: uuid.UUID | None = None) -> str:
    raw = (u or uuid.uuid4()).bytes
    return base64.urlsafe_b64encode(raw).decode()[:8]


def is_task_id(s: object) -> bool:
    return isinstance(s, str) and bool(TASK_ID.match(s))

"""The public edge's path allow-list — what the internet may reach at all.

On the public profile (``AWM_EDGE_PROFILE=public``) the front stops being a
transparent proxy for the whole gateway and becomes a door for two apps. This
module is the single statement of that door: a request whose path is not
listed here is a 404 whether or not it carries a session, so nothing else in
awm — the hub control plane, ``/invoke``, other services, the fileviewer root —
is even discoverable from outside.

Three answers per path:

* ``DENY``  — not part of the public surface. 404.
* ``OPEN``  — allowed for any authenticated session.
* ``USER``  — allowed only when the path names the session's own user (a
  user-prefixed emit topic, the user's own ``/files`` subtree).

The lists are constants with tests, not a per-host environment string, so a
change to the surface is a reviewed diff.
"""

from __future__ import annotations

import re
from enum import Enum


class Verdict(str, Enum):
    DENY = "deny"
    OPEN = "open"
    USER = "user"


# Service verbs the browser pages call. Everything a page does not need — bulk
# reindex/purge, the agent checkout workflow, raw path access, exports that
# need Docker/Chrome — stays off the public surface.
NOTES_DENIED_FNS = frozenset({
    "reindex", "purge", "checkout", "checkouts", "path", "read", "write",
    "status", "update", "resolve", "merge", "discard",
})
DRAWIO_DENIED_FNS = frozenset({
    "export", "url", "checkout", "edit", "externalize", "path", "status",
    "update", "resolve", "merge", "discard", "checkouts", "import",
    "status_service",
})
DRAWIO_DENIED_PREFIXES = ("autopublish",)

OPEN_PREFIXES = ("/ui/notes/", "/ui/drawio/", "/drawio-app/")
OPEN_EXACT = frozenset({"/", "/ui/notes", "/ui/drawio", "/drawio-app",
                        "/__auth/login", "/__auth/logout", "/__auth/whoami"})
# The view mount renders with headless Chrome, which the public host lacks.
DENIED_PREFIXES = ("/drawio-app/view",)

_FN = re.compile(r"^/svc/(notes|drawio)/fn/([^/]+)$")
_EMIT = re.compile(r"^/svc/(notes|drawio)/emit/(.+)$")
_FILES = re.compile(r"^/files/projects/userdata/([^/]+)(?:/.*)?$")

# Topic prefixes the services use for a bound user (see notes/drawio rooms).
_TOPIC_PREFIX = {"notes": "note", "drawio": "drawio"}


def classify(path: str) -> Verdict:
    """Static verdict for ``path`` before any identity is known."""
    if path in OPEN_EXACT:
        return Verdict.OPEN
    if path.startswith(DENIED_PREFIXES):
        return Verdict.DENY
    if path.startswith(OPEN_PREFIXES):
        return Verdict.OPEN
    m = _FN.match(path)
    if m:
        svc, fn = m.groups()
        if svc == "notes":
            return Verdict.DENY if fn in NOTES_DENIED_FNS else Verdict.OPEN
        if fn in DRAWIO_DENIED_FNS or fn.startswith(DRAWIO_DENIED_PREFIXES):
            return Verdict.DENY
        return Verdict.OPEN
    if _EMIT.match(path) or _FILES.match(path):
        return Verdict.USER
    return Verdict.DENY


def allows(path: str, sub: str | None) -> bool:
    """Whether an authenticated session acting as ``sub`` may reach ``path``."""
    verdict = classify(path)
    if verdict is Verdict.OPEN:
        return True
    if verdict is Verdict.DENY or not sub:
        return False
    m = _EMIT.match(path)
    if m:
        svc, topic = m.groups()
        return topic.startswith(f"{_TOPIC_PREFIX[svc]}:{sub}:")
    m = _FILES.match(path)
    return bool(m) and m.group(1) == sub

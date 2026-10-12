"""Owner checks: a post's literal owner must be a scope the scopes service knows.

journal holds no scope table, so it asks the scopes service through the
``resolveScope`` RPC and remembers a positive answer for a minute. A scopes
service that cannot answer does not stop the post: the check only exists to
catch a mistyped scope, and a debrief written during an outage is worth more.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("awm.journal.owners")

TTL_S = 60.0
TIMEOUT_S = 5.0

_known: dict[tuple[str, str], float] = {}


def scope_siblings(project: str, scope: str) -> list[str]:
    """The project's other live scopes, by name, as the scopes service lists them."""
    from awm.gatewayclient import call_sync
    res = call_sync("scopes", "scope_search",
                    {"project": project, "status": "active", "limit": 1000}, timeout=TIMEOUT_S)
    return [s["scope"] for s in res.get("scopes", []) if s.get("scope") != scope]


def require_scope(project: str, scope: str) -> None:
    """Raise ``ValueError`` when ``project/scope`` is a literal owner that does not exist.

    A non-literal channel (``project=''``) names a user, a project or the
    workspace and has no scope to check.
    """
    if not project:
        return
    key = (project, scope)
    if _known.get(key, 0.0) > time.monotonic():
        return
    try:
        from awm.gatewayclient import call_sync
        res = call_sync("scopes", "resolveScope", {"project": project, "scope": scope},
                        timeout=TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — see the module docstring
        log.warning("resolveScope %s/%s unavailable, accepting the post: %s", project, scope, exc)
        return
    if not (isinstance(res, dict) and res.get("exists")):
        raise ValueError(f"no scope {project}/{scope}")
    _known[key] = time.monotonic() + TTL_S

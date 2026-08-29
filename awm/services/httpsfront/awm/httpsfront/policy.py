"""The public edge's path allow-list — what the internet may reach at all.

On the public profile (``AWM_EDGE_PROFILE=public``) the front stops being a
transparent proxy for the whole gateway and becomes a door for a named few
things. This module is the single statement of that door: a request whose path
is not listed here is a 404 whether or not it carries a session, so nothing
else in awm — the hub control plane, ``/invoke``, other services, the
fileviewer root — is even discoverable from outside.

Five answers per path:

* ``DENY``  — not part of the public surface. 404.
* ``OPEN``  — allowed for any authenticated session.
* ``USER``  — allowed only when the path names the session's own user (a
  user-prefixed emit topic, the user's own ``/files`` subtree).
* ``VAULT`` — the shared knowledge base, allowed for any session belonging to a
  person. A fourth verdict rather than ``USER`` because the two mean opposite
  things: ``USER`` admits a path that *names* the caller, and a vault path names
  nobody at all. Reusing ``USER`` would make that distinction untestable.
* ``PENPOT`` — Penpot's own root-level frontend paths, gated the same way as
  ``VAULT`` and for the same reason: a person's design files, not a machine's.

The lists are constants with tests, not a per-host environment string, so a
change to the surface is a reviewed diff.
"""

from __future__ import annotations

import re
from enum import Enum

from awm.httpsfront import penpot, vault
from awm.httpsfront.auth import PEER_SUB


class Verdict(str, Enum):
    DENY = "deny"
    OPEN = "open"
    USER = "user"
    VAULT = "vault"
    PENPOT = "penpot"


# Service verbs the browser pages call. Everything a page does not need — bulk
# reindex/purge, the agent checkout workflow, raw path access, exports that
# need Docker/Chrome — stays off the public surface.
DRAWIO_DENIED_FNS = frozenset({
    "export", "checkout", "edit", "externalize", "path", "status",
    "update", "resolve", "merge", "discard", "checkouts", "import",
    "status_service",
})
DRAWIO_DENIED_PREFIXES = ("autopublish",)

#: "/penpot" and "/penpot-view" are listed explicitly rather than left to the
#: early VAULT/PENPOT classify() branches (or, for the view mount, to falling
#: through to a generic svc-routed verdict) — a forward-compat requirement
#: from the public-sirius integrator's branch, where this list is the *only*
#: gate: an unlisted prefix 404s there regardless of anything else in this
#: module. See test_penpot_paths.py.
#: Written slashed, with the bare form in ``OPEN_EXACT`` beside it, because
#: this is a ``startswith`` against a deny-by-default door: a bare "/penpot"
#: here opens every path merely *starting* with those eight characters, so a
#: future "/penpot-admin" would be reachable from the internet by having a
#: name, which is the one thing this module exists to prevent. Penpot's own
#: paths never needed it -- ``penpot.owns()`` claims them a branch earlier.
OPEN_PREFIXES = ("/ui/drawio/", "/drawio-app/", "/ui/trilium/",
                 "/penpot/", "/penpot-view/")
OPEN_EXACT = frozenset({"/", "/ui/drawio", "/drawio-app", "/ui/trilium",
                        "/penpot", "/penpot-view",
                        "/__auth/login", "/__auth/logout", "/__auth/whoami"})

# The vault's own verbs. An allow-list, opposite to the deny-lists above,
# because this surface is small and closed and most of it is destructive: the
# vault is shared, so `restore` discards everyone's work and `export` rebuilds
# the whole tree on a two-core box. A verb added later is unreachable until
# somebody adds it here on purpose.
#
# This is defence in depth, not the enforcement. A mesh node's edge runs no
# profile at all and never consults this module, so the real gate is
# `_operator_only` in the trilium service. Both, because the day one of them is
# wrong should not also be the day the other was the only one.
TRILIUM_OPEN_FNS = frozenset({"status", "snapshots", "url"})
# The view mount renders with headless Chrome, which the public host lacks.
DENIED_PREFIXES = ("/drawio-app/view",)

_FN = re.compile(r"^/svc/(drawio|trilium)/fn/([^/]+)$")
_EMIT = re.compile(r"^/svc/(drawio)/emit/(.+)$")
_FILES = re.compile(r"^/files/projects/userdata/([^/]+)(?:/.*)?$")

# Topic prefixes the services use for a bound user (see the drawio rooms).
# Must stay in step with the alternatives in ``_EMIT``: ``allows`` indexes this.
_TOPIC_PREFIX = {"drawio": "drawio"}


def classify(path: str) -> Verdict:
    """Static verdict for ``path`` before any identity is known."""
    # Before the exact list, so the vault's own paths are not shadowed by
    # anything here — and after nothing, so ``/`` is untouched.
    if vault.owns(path):
        return Verdict.VAULT
    # Same reasoning, same position, for Penpot. The two mounts are disjoint
    # by construction now that each has a prefix of its own, so the order
    # between them decides nothing.
    if penpot.owns(path):
        return Verdict.PENPOT
    if path in OPEN_EXACT:
        return Verdict.OPEN
    if path.startswith(DENIED_PREFIXES):
        return Verdict.DENY
    if path.startswith(OPEN_PREFIXES):
        return Verdict.OPEN
    m = _FN.match(path)
    if m:
        svc, fn = m.groups()
        if svc == "trilium":
            return Verdict.OPEN if fn in TRILIUM_OPEN_FNS else Verdict.DENY
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
    if verdict is Verdict.VAULT:
        # A person, not a machine. A peer bearer is another node's process and
        # has no business in a human's knowledge base; `operator` is the shared
        # -password session, which this profile does not issue anyway.
        return sub not in (PEER_SUB, "operator")
    if verdict is Verdict.PENPOT:
        # Same exclusion, same reason: a peer/operator machine bearer has no
        # business in a person's design files.
        return sub not in (PEER_SUB, "operator")
    m = _EMIT.match(path)
    if m:
        svc, topic = m.groups()
        return topic.startswith(f"{_TOPIC_PREFIX[svc]}:{sub}:")
    m = _FILES.match(path)
    return bool(m) and m.group(1) == sub

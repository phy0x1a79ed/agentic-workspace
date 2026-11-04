"""The public edge's path allow-list — what the internet may reach at all.

On the public profile (``AWM_EDGE_PROFILE=public``) the front stops being a
transparent proxy for the whole gateway and becomes a door for a named few
things. This module is the single statement of that door: a request whose path
is not listed here is a 404 whether or not it carries a session, so nothing
else in awm — the hub control plane, ``/invoke``, other services, the
fileviewer root — is even discoverable from outside.

Seven answers per path:

* ``DENY``  — not part of the public surface. 404.
* ``OPEN``  — allowed for any authenticated session.
* ``USER``  — allowed only when the path names the session's own user (the
  user's own ``/files`` subtree).
* ``VAULT`` — the shared knowledge base, allowed for any session belonging to a
  person. A fourth verdict rather than ``USER`` because the two mean opposite
  things: ``USER`` admits a path that *names* the caller, and a vault path names
  nobody at all. Reusing ``USER`` would make that distinction untestable.
* ``SLICE`` — a public slice of the vault: one note and its descendants, opened
  by a link to somebody with no awm account. The one verdict that is allowed
  with **no session at all**, which is why it is a verdict of its own rather
  than a prefix in ``OPEN_PREFIXES``: everything else here narrows what an
  authenticated caller may reach, and this widens the door to an unauthenticated
  one. What keeps that narrow is that the token names the slice, the trilium
  service decides whether it exists, and Trilium's own mask refuses every note
  outside it — see :mod:`awm.httpsfront.slices`. Deliberately not a member of
  ``OPEN_PREFIXES``: entries there are ``startswith`` matches, so listing the
  mount would classify the paths a slice refuses as OPEN and forward them to the
  *gateway*. A branch where that list is the only gate needs this verdict as
  well, not the prefix on its own.
* ``TETHER`` — the remote-assistance relay. The **second** verdict allowed with
  no session at all, and the reason is the tool's whole point: the person on the
  other end is a friend being helped with their own machine, and one who needed
  an awm account first is one this could not help. What keeps it narrow is not
  trust in that person but the relay's own shape — it refuses the two operator
  routes without its own bearer, answers every refusal with an identical 404 so
  its surface cannot be mapped by the shape of a decline, and issues a session
  only to an authenticated operator, so nothing reachable here can be brought
  into existence from outside. The mount is an allow-list of exact path grammars
  rather than a prefix, so a slot the relay never issued is turned away here
  before it costs a socket. See :mod:`awm.httpsfront.tether`.
* ``BOARD`` — the federation board, the **third** verdict allowed with no edge
  session. Callers are parties of the board, other swarms among them, and the
  board authenticates their own bearer, which the edge passes through unread.
  Like ``TETHER`` it is an allow-list of exact shapes (see
  :mod:`awm.httpsfront.board`), and a path inside the mount that no shape
  claims is ``DENY``. Whether a board is wired at all is the proxy's question.

The lists are constants with tests, not a per-host environment string, so a
change to the surface is a reviewed diff.
"""

from __future__ import annotations

import re
from enum import Enum

from awm.httpsfront import board, slices, tether, vault
from awm.httpsfront.auth import is_machine_sub

class Verdict(str, Enum):
    DENY = "deny"
    OPEN = "open"
    USER = "user"
    VAULT = "vault"
    SLICE = "slice"
    TETHER = "tether"
    BOARD = "board"


#: Written slashed, with the bare form in ``OPEN_EXACT`` beside it, because
#: this is a ``startswith`` against a deny-by-default door: a bare "/ui/trilium"
#: here opens every path merely *starting* with those characters, so a future
#: "/ui/trilium-admin" would be reachable from the internet by having a name,
#: which is the one thing this module exists to prevent.
OPEN_PREFIXES = ("/ui/trilium/",)
OPEN_EXACT = frozenset({"/", "/ui/trilium",
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
_FN = re.compile(r"^/svc/trilium/fn/([^/]+)$")
_FILES = re.compile(r"^/files/projects/userdata/([^/]+)(?:/.*)?$")


def classify(path: str) -> Verdict:
    """Static verdict for ``path`` before any identity is known."""
    # Before the exact list, so the vault's own paths are not shadowed by
    # anything here — and after nothing, so ``/`` is untouched.
    if vault.owns(path):
        return Verdict.VAULT
    # Beside the vault because it is the same upstream, and before the exact
    # and prefix lists for the same reason. A path under the mount that
    # ``owns`` refused falls through to DENY on its own: no "/slice/" entry
    # appears in any list below.
    if slices.owns(path):
        return Verdict.SLICE
    # Same position, same reasoning: a separate upstream whose paths must not
    # be shadowed by anything below. Both branches are needed and neither is
    # redundant — `owns` is an allow-list of exact shapes, so `refused` is what
    # keeps a *near*-miss inside the mount from falling through to the gateway.
    if tether.owns(path):
        return Verdict.TETHER
    if tether.refused(path):
        return Verdict.DENY
    # The board's mount, same position and same reasoning as the tether: a
    # separate upstream whose paths nothing below may shadow, and whose near
    # misses must not fall through to the gateway. The verdict is path-only;
    # the method is checked where the request is forwarded.
    if board.owns(path):
        return Verdict.BOARD
    if board.in_mount(path):
        return Verdict.DENY
    if path in OPEN_EXACT:
        return Verdict.OPEN
    if path.startswith(OPEN_PREFIXES):
        return Verdict.OPEN
    m = _FN.match(path)
    if m:
        return Verdict.OPEN if m.group(1) in TRILIUM_OPEN_FNS else Verdict.DENY
    if _FILES.match(path):
        return Verdict.USER
    return Verdict.DENY


def allows(path: str, sub: str | None) -> bool:
    """Whether an authenticated session acting as ``sub`` may reach ``path``."""
    verdict = classify(path)
    if verdict is Verdict.OPEN:
        return True
    if verdict is Verdict.SLICE:
        # No subject, by design: a slice is reached by holding the link. The
        # token is the credential, and it is checked where credentials are
        # kept rather than here.
        return True
    if verdict is Verdict.TETHER:
        # No subject either, and for a sharper reason: the caller is the person
        # being helped, on their own machine, and requiring an awm account of
        # them would defeat the tool. The relay does its own gating — see the
        # module docstring and :mod:`awm.httpsfront.tether`.
        return True
    if verdict is Verdict.BOARD:
        # No subject: the caller is a party of the board, which authenticates
        # its own bearer. See :mod:`awm.httpsfront.board`.
        return True
    if verdict is Verdict.DENY or not sub:
        return False
    if verdict is Verdict.VAULT:
        # A person, not a machine. A peer (the legacy bearer or a verified
        # `peer:<node>`) is another node's process and has no business in a
        # human's knowledge base; `operator` is the shared-password session,
        # which this profile does not issue anyway.
        return not is_machine_sub(sub)
    m = _FILES.match(path)
    return bool(m) and m.group(1) == sub

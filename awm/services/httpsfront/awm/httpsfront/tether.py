"""Which paths belong to the tether relay, and why they need no awm session.

The relay is the third upstream on this edge that is not the gateway, and the
only one whose whole purpose is to be reached by somebody with no account here.
That is the tool: the operator mints an invite, reads two words to a friend over
the phone, and the friend runs one line on their own machine. A friend who had
to have an awm login first would be a friend this tool could not help.

**So the mount is unauthenticated, and the relay is what gates it.** Nothing is
delegated on trust: the relay refuses ``/issue`` and ``/status`` without its own
bearer, answers every refusal with the same 404 so its surface cannot be mapped
by the shape of a decline, and — the property everything else rests on — a
session exists there only because an authenticated operator asked for one. An
unauthenticated caller cannot bring anything into existence, so what is exposed
here is a door onto sessions that already exist and are already addressed by a
number the relay itself issued.

**The mount is an allow-list of exact shapes, not a prefix.** The relay's whole
public surface is seven routes and every one of them has a fixed grammar, so the
edge can refuse everything else before it has consulted anything at all — which
is the plan's requirement that a slot the relay never issued be turned away
before any pairing happens. The shapes have to agree exactly with what the relay
parses (``Slot::parse``: one to three digits, no leading zero, 1 to 999;
``Token::parse``: thirty-two lowercase hex) or the edge would forward a request
the relay is about to reject, which costs nothing but says the two disagree.

**What is deliberately not forwarded:**

  - ``/health`` — the relay's liveness probe. Whatever supervises that process
    reaches it over loopback; the internet has no use for it and it is the one
    route that answers the same way whether or not anything is configured.
  - everything else under the mount — refused explicitly rather than left to
    fall through, because a path inside a mount that the mount does not claim
    would otherwise be proxied to the *gateway* instead.

**Nothing secret travels here.** The slot is in the URL and lands in every
access log on the path, and that is by design: it authenticates nobody. The
phrase — the actual credential — never leaves the two machines that hold it, and
the ticket in a join URL is one attempt at one chair on a session already named
by the slot beside it.
"""

from __future__ import annotations

import re

#: The mount. No trailing slash, because the bare path *is* a route: it serves
#: the launcher script, and ``curl … | bash`` is the whole of what a person is
#: told to do with this address.
PREFIX = "/tether"

_PREFIX_BYTES = PREFIX.encode("ascii")

#: One slot, as the relay parses it: one to three digits, no leading zero.
_SLOT = r"[1-9][0-9]{0,2}"

#: One seat ticket, as the relay parses it.
_TOKEN = r"[0-9a-f]{32}"

#: A downloadable name, matching the relay's own asset allow-list.
_ASSET = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"

#: Every path this mount claims. Anything else under the mount is refused.
SHAPES = (
    re.compile(rf"^{PREFIX}$"),                            # the launcher script
    re.compile(rf"^{PREFIX}/win$"),                        # the same, for Windows
    re.compile(rf"^{PREFIX}/bin/{_ASSET}$"),               # a client download
    re.compile(rf"^{PREFIX}/claim/{_SLOT}$"),              # the owner's ticket
    re.compile(rf"^{PREFIX}/join/{_SLOT}/{_TOKEN}$"),      # the session socket
    re.compile(rf"^{PREFIX}/issue$"),                      # operator only, at the relay
    re.compile(rf"^{PREFIX}/status$"),                     # operator only, at the relay
)

#: Routes the relay serves that we do not forward, with the reason. Recorded
#: rather than merely absent so the next reader sees a decision, and so a test
#: can assert each one stays unreachable.
NOT_FORWARDED = {
    "/health": "a liveness probe for whatever supervises the process, over loopback",
}


def owns(path: str) -> bool:
    """Whether ``path`` is served by the relay rather than by the gateway."""
    return any(shape.match(path) for shape in SHAPES)


def refused(path: str) -> bool:
    """Whether ``path`` is inside the mount but not one this mount claims.

    Answered separately from :func:`owns` because the two have different
    callers: the policy door turns this into a DENY, and the proxy turns it
    into a 404 on a node that runs no policy at all. Without it such a path
    falls through and is proxied to the gateway.
    """
    return (path == PREFIX or path.startswith(PREFIX + "/")) and not owns(path)


def upstream_path(path: str) -> str:
    """The path to ask the relay for: ``path`` with the mount taken off.

    The bare mount becomes ``/``, which the relay answers with the launcher —
    it serves that script at both ``/`` and ``/tether`` precisely so this
    rewrite can be the obvious one rather than a special case.
    """
    if path == PREFIX:
        return "/"
    if path.startswith(PREFIX + "/"):
        return path[len(PREFIX):]
    return path


def upstream_raw_path(raw: bytes) -> bytes | None:
    """:func:`upstream_path` on the bytes as they arrived, or ``None``.

    The edge routes on the *decoded* path and forwards the *raw* one, so the
    mount has to be present in both. ``None`` says "route said yes, bytes say
    no", which the caller answers with a 404 like any other path off the list.
    """
    if raw == _PREFIX_BYTES:
        return b"/"
    if not raw.startswith(_PREFIX_BYTES + b"/"):
        return None
    return raw[len(_PREFIX_BYTES):]

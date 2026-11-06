"""Which paths belong to the tether relay, and why they need no awm session.

The relay is the third upstream on this edge that is not the gateway, and the
only one whose whole purpose is to be reached by somebody with no account here.
That is the tool: the operator mints an invite, reads two words to a friend over
the phone, and the friend runs one line on their own machine. A friend who had
to have an awm login first would be a friend this tool could not help.

**So the mount is unauthenticated, and nothing here creates anything.** A slot
and a room come into existence only on the relay's own host: its ``relay``
service holds the bearer for ``/issue``, ``POST /room`` and ``/status``, and a
member node asks it through its door (``channels.md`` R147). This edge forwards
none of those three routes. What is exposed here is a door onto sessions and
rooms that already exist and are already addressed by a name the relay itself
issued, and the relay answers every refusal with the same 404 so its surface
cannot be mapped by the shape of a decline.

**The mount is an allow-list of exact shapes, not a prefix.** The relay's public
surface is a short list of routes and every one of them has a fixed grammar, so the
edge can refuse everything else before it has consulted anything at all — which
is the plan's requirement that a slot the relay never issued be turned away
before any pairing happens. The shapes have to agree exactly with what the relay
parses (``Slot::parse``: one to three digits, no leading zero, 1 to 999;
``Token::parse``: thirty-two lowercase hex, which a poll id is too;
``room::parse_name``: three or six lowercase list words joined by hyphens) or the edge
would forward a request
the relay is about to reject, which costs nothing but says the two disagree.

**Rooms have a mount of their own.** A link spells out its kind, so a room is
``/room/<name>`` at the edge root and a machine stays under ``/tether``. The
relay serves rooms at the same ``/room`` paths, so that mount is forwarded with
its path unchanged.

**What is deliberately not forwarded:**

  - ``/health`` — the relay's liveness probe. Whatever supervises that process
    reaches it over loopback; the internet has no use for it and it is the one
    route that answers the same way whether or not anything is configured.
  - ``/issue``, ``/status`` and ``POST /room`` — the bearer's routes. The bearer
    never leaves the relay's host, so nothing on the internet could use them.
  - everything else under the mount — refused explicitly rather than left to
    fall through, because a path inside a mount that the mount does not claim
    would otherwise be proxied to the *gateway* instead.

**Nothing secret travels here.** The slot is in the URL and lands in every
access log on the path, and that is by design: it authenticates nobody. The
phrase — the actual credential — never leaves the two machines that hold it, and
the ticket in a join URL is one attempt at one chair on a session already named
by the slot beside it.

The one qualification is the token on ``/release`` and ``/hold``, which is the
operator's seat token rather than a one-shot ticket: it is reusable for the life of its slot, and
anyone holding it could already take that chair. It is minted by the relay, it
never identifies a person, and it dies with the slot — which that request is
asking for. Worth knowing when reading an access log, not worth a second
channel. The same goes for the owner's seat token in a join URL once the slot is
held: it lets the owner back into its own chair, and only the resume secret,
which never leaves the sealed channel, lets either end speak there.
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

#: One polled chair, named by the id the relay handed back when it was taken.
#: Minted as a token, so it has a token's grammar.
_POLL_ID = _TOKEN

#: The rooms' mount, beside this one at the edge root. The relay serves it at the
#: same path, so it is forwarded unchanged.
ROOMS = "/room"

_ROOMS_BYTES = ROOMS.encode("ascii")

#: One room, named as the relay mints it: three words of the tether list for a
#: task room, six for a persistent one, lowercase, joined by hyphens. Every list
#: word is three to eight letters. Whether each is on the list is the relay's to
#: answer, as whether a slot is live is.
_WORD = r"[a-z]{3,8}"
_ROOM = rf"{_WORD}(?:-{_WORD}){{2}}(?:(?:-{_WORD}){{3}})?"

#: A downloadable name, matching the relay's own asset allow-list.
_ASSET = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"

#: Every path this mount claims. Anything else under the mount is refused.
SHAPES = (
    re.compile(rf"^{PREFIX}$"),                            # the launcher script
    re.compile(rf"^{PREFIX}/win$"),                        # the same, for Windows
    re.compile(rf"^{PREFIX}/bin/{_ASSET}$"),               # a client download
    re.compile(rf"^{PREFIX}/claim/{_SLOT}$"),              # the owner's ticket
    re.compile(rf"^{PREFIX}/join/{_SLOT}/{_TOKEN}$"),      # the session socket
    re.compile(rf"^{PREFIX}/poll/{_SLOT}/{_TOKEN}$"),      # the same chair, for a path with no socket
    re.compile(rf"^{PREFIX}/seat/{_POLL_ID}/(?:recv|send|close)$"),  # and its requests
    re.compile(rf"^{PREFIX}/release/{_SLOT}/{_TOKEN}$"),   # the operator ending its own slot
    re.compile(rf"^{PREFIX}/hold/{_SLOT}/{_TOKEN}$"),      # the operator holding it for a consented owner
    re.compile(rf"^{ROOMS}/{_ROOM}$"),                     # a room's how-to, for its guest
    re.compile(rf"^{ROOMS}/{_ROOM}/(?:send|recv|ws|leave)$"),  # and its requests
)

#: Routes the relay serves that we do not forward, with the reason. Recorded
#: rather than merely absent so the next reader sees a decision, and so a test
#: can assert each one stays unreachable.
NOT_FORWARDED = {
    "/health": "a liveness probe for whatever supervises the process, over loopback",
    "/issue": "mints a slot behind the bearer, which never leaves the relay's host",
    "/status": "the operator's report, behind the same bearer",
    "/room": "opens a room behind the same bearer",
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
    inside = any(path == m or path.startswith(m + "/") for m in (PREFIX, ROOMS))
    return inside and not owns(path)


def upstream_path(path: str) -> str:
    """The path to ask the relay for: ``path`` with the mount taken off.

    The bare mount becomes ``/``, which the relay answers with the launcher —
    it serves that script at both ``/`` and ``/tether`` precisely so this
    rewrite can be the obvious one rather than a special case.
    """
    if path == PREFIX:
        return "/"
    if path == ROOMS or path.startswith(ROOMS + "/"):
        return path
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
    if raw == _ROOMS_BYTES or raw.startswith(_ROOMS_BYTES + b"/"):
        return raw
    if not raw.startswith(_PREFIX_BYTES + b"/"):
        return None
    return raw[len(_PREFIX_BYTES):]

"""Which paths belong to the federation board, and why they need no awm session.

The board is the one upstream on this edge that other swarms call as themselves.
A foreign party posts a card, claims one and follows the stream, and it does so
with a bearer the board issued to that party. The edge therefore has no identity
to offer and none to check: **the board is what authenticates**, with its own
party table, and it answers every refusal with the same 404 so its surface
cannot be mapped by the shape of a decline.

That is the tether's arrangement, and for the same structural reason the mount
is answered before the edge's own authentication. The edge does not check the
``Authorization`` header either: it is a credential for the board, and an edge
that checked it would be a second place to get that check wrong. It looks at
the header only to keep a mesh credential (a node token or the legacy node-wide
bearer) from reaching the board, and refuses a request carrying two. What the edge
adds is narrower than a login and cheaper than a socket: an allow-list of exact
path shapes, a body-size cap, and the removal of every header that claims an
identity the edge did not verify.

**The mount is an allow-list of exact method-and-path shapes, not a prefix.** A
card id is thirty-two lowercase hex digits, the form the board mints. The board
itself also accepts the dashed UUID spelling; the edge does not, so the public
surface is narrower than the service behind it. The shapes have to agree with
the routes in ``awm.board.http`` or the edge would forward a request the board
is about to refuse, which costs nothing but says the two disagree.

**What is deliberately not forwarded:** anything else under ``/board``, which
is refused here rather than left to fall through to the gateway.
"""

from __future__ import annotations

import re

#: The mount. The board serves the same prefix, so the path is forwarded as is.
PREFIX = "/board"

#: A card id as the board mints it.
_ID = r"[0-9a-f]{32}"

#: Every request this mount claims, as ``(methods, path)``. Anything else under
#: the mount is refused, as is a claimed path with another method.
SHAPES: tuple[tuple[frozenset[str], re.Pattern[str]], ...] = (
    (frozenset({"POST", "GET"}), re.compile(rf"^{PREFIX}/cards$")),
    (frozenset({"GET"}), re.compile(rf"^{PREFIX}/cards/{_ID}$")),
    (frozenset({"POST"}), re.compile(rf"^{PREFIX}/cards/{_ID}/(?:claim|complete|fail)$")),
    (frozenset({"GET"}), re.compile(rf"^{PREFIX}/stream$")),
)

#: Largest request body forwarded. The board caps card text at 200,000
#: characters; this leaves room for JSON escaping and nothing more.
MAX_BODY = 1024 * 1024


def owns(path: str) -> bool:
    """Whether ``path`` has a shape this mount claims, for some method."""
    return any(pattern.fullmatch(path) for _, pattern in SHAPES)


def allows(method: str, path: str) -> bool:
    """Whether this mount claims ``method`` on ``path``."""
    method = method.upper()
    return any(method in methods and pattern.fullmatch(path) for methods, pattern in SHAPES)


def in_mount(path: str) -> bool:
    """Whether ``path`` is at or under the mount, claimed or not.

    The edge answers for the whole mount whether or not a board is wired, so a
    path the mount does not claim never falls through to the gateway.
    """
    return path == PREFIX or path.startswith(PREFIX + "/")


def upstream_raw_path(raw: bytes) -> bytes | None:
    """The path to ask the board for: ``raw`` itself, when the bytes are a claimed shape.

    The edge routes on the decoded path and forwards the raw one. An encoded
    spelling of a claimed path (a percent-escaped hex digit, say) decodes to a
    shape but is not one as sent, and is refused: ``None``, which the caller
    answers with a 404 like any other path off the list.
    """
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    return raw if owns(text) else None

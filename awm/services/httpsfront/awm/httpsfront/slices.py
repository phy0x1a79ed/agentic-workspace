"""Which paths belong to a public slice of the vault, and what a slice may reach.

A slice is one note and its descendants, opened to somebody with no awm account.
It is served by the same Trilium child as :mod:`vault`, from a second mount whose
first segment after the prefix is the token that names it::

    /slice/<token>/            -> the vault's /
    /slice/<token>/api/tree    -> the vault's /api/tree

**The token is in the path, not the query, for three reasons.** Trilium's shell
references everything relatively, so the mount has to be a directory for the same
reason ``/trilium/`` does -- see :mod:`vault`. A query string is dropped by the
first relative reference the shell resolves, so a token there would survive
exactly one request. And two slices open in one browser get one cookie path each,
so the visitor names they carry cannot collide.

**The trailing slash is load-bearing**, identically to the vault: a slash-less
mount resolves every relative reference one level too high, and Trilium's own
hashchange parser wants the literal ``/#root``. The edge answers the bare form
with a 308.

**What this module does not decide.** Whether a token exists, what note it opens,
who is visiting and whether they may write are all answered by the trilium
service, over the gateway, per request -- a token is a credential and this
process holds no credentials. Nor does this module bound what a slice may *see*:
the mask that refuses a note outside the slice lives in Trilium itself, ahead of
its API router, because only it can ask whether one note descends from another.
This module answers one question: does this path belong to a slice, and what is
its path inside the vault?

**What is deliberately not forwarded.** A slice reaches strictly less than a
signed-in person does, so its refusal list starts as the vault's -- every entry
there is refused here for the reason recorded there -- and adds the routes that
only mean something to somebody who has an account:

  - ``/manifest.webmanifest``, ``/robots.txt`` -- the vault synthesizes the first
    for its own mount and neither belongs to a link handed to one person.

The list is a refusal on top of an allow-list, not a deny-list on its own: the
prefix is the allow-list and it is closed by construction, and inside Trilium
every route the mask does not name is refused as well.
"""

from __future__ import annotations

import re

from awm.httpsfront import vault

#: The mount. Everything under it is a slice; the segment after it is the token.
PREFIX = "/slice/"

_PREFIX_BYTES = PREFIX.encode("ascii")

#: A token as :func:`token_of` will accept it. ``secrets.token_urlsafe`` emits
#: exactly this alphabet, and the bound form is a path segment like any other:
#: anything else is not a token we minted, so it is not a slice.
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

#: Refused on top of :data:`vault.NOT_FORWARDED`, which a slice inherits whole.
#: An entry ending in "/" matches a prefix, as it does there.
NOT_FORWARDED = {
    "/manifest.webmanifest": "the vault synthesizes its own; a slice installs as nothing",
    "/robots.txt": "a link handed to one person is not a site to crawl",
}

#: What :func:`owns` actually matches against. Merged rather than chained so a
#: test can read the whole refusal in one place.
ALL_NOT_FORWARDED = {**vault.NOT_FORWARDED, **NOT_FORWARDED}

#: The three headers the edge stamps on a forwarded slice request, and strips
#: from an inbound one. Trilium trusts them exactly as much as it trusts
#: loopback, which is total: the child binds loopback and this edge is the only
#: route in.
HEADER_ROOT = "X-Awm-Slice-Root"
HEADER_USER = "X-Awm-Slice-User"
HEADER_WRITE = "X-Awm-Slice-Write"

#: Prefix of the cookie naming the visitor of an open token, once they have
#: arrived with ``?user=``. The token is in the name as well as the cookie's
#: path: a browser sends every cookie whose path matches, and a parser that
#: keeps one value per name would otherwise hand one slice another's visitor.
COOKIE_PREFIX = "awm_slice_user_"

#: The query parameter that names the visitor, kept in the URL rather than
#: swapped for the cookie so that who a link attributes to stays visible in the
#: address bar.
USER_PARAM = "user"


def cookie_name(token: str) -> str:
    """The cookie naming the visitor of the open slice ``token``."""
    return COOKIE_PREFIX + token


def token_of(path: str) -> str | None:
    """The token ``path`` names, or ``None`` if it names none."""
    if not path.startswith(PREFIX):
        return None
    token = path[len(PREFIX):].split("/", 1)[0]
    return token if _TOKEN.match(token) else None


def owns(path: str) -> bool:
    """Whether ``path`` is served as a slice of the vault."""
    token = token_of(path)
    if token is None:
        return False
    if path == PREFIX + token:
        return True
    return not _refused(upstream_path(path))


def refused(path: str) -> bool:
    """Whether ``path`` names a slice but a route inside it we do not forward.

    Distinct from :func:`owns` because the caller has to answer it rather than fall through: a mesh
    node's edge runs no profile and consults no allow-list, so without this an ``/etapi/`` path
    inside the mount reaches the ordinary authentication check and answers 401 — telling a stranger
    that the path exists and that a session would reach it.
    """
    return token_of(path) is not None and not owns(path)


def shell_bare(token: str) -> str:
    """The slash-less mount, which the edge answers with a 308 to :func:`shell`."""
    return PREFIX + token


def shell(token: str) -> str:
    """Where the application shell is served for ``token``."""
    return PREFIX + token + "/"


def upstream_path(path: str) -> str:
    """The path to ask the vault for: ``path`` with the mount and token taken off.

    The single rewrite, as in :func:`vault.upstream_path`, and for the same
    reason: the HTTP leg, the WebSocket leg and the refusal list above have to
    agree on what "inside the slice" means.
    """
    token = token_of(path)
    if token is None:
        return path
    return path[len(PREFIX) + len(token):] or "/"


def upstream_raw_path(raw: bytes) -> bytes | None:
    """:func:`upstream_path` on the bytes as they arrived, or ``None``.

    The edge routes on the decoded path and forwards the raw one, so a target
    whose mount only appears after percent-decoding classifies as a slice's and
    must not then be forwarded with the mount still attached. ``None`` says
    "route said yes, bytes say no", which the caller answers with a 404.
    """
    if not raw.startswith(_PREFIX_BYTES):
        return None
    rest = raw[len(_PREFIX_BYTES):]
    token, slash, tail = rest.partition(b"/")
    if not _TOKEN.match(token.decode("latin-1")):
        return None
    if not slash:
        return b"/"
    return b"/" + tail


def _refused(inner: str) -> bool:
    """Whether a path inside a slice is one we do not forward."""
    for entry in ALL_NOT_FORWARDED:
        if entry.endswith("/"):
            if inner.startswith(entry):
                return True
        elif inner == entry or inner.startswith(entry + "/"):
            return True
    return False

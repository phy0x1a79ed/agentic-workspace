"""Which paths belong to the shared knowledge base, and where its shell lives.

The vault is the one upstream on this edge that is not the gateway. There is
exactly one of it, shared by everyone who can sign in, so this module answers
only "does this path belong to the vault?" — never "whose vault?", because that
question has no answer here and no caller can pose it.

**One prefix, and the trailing slash is load-bearing.** Trilium has no URL-base
setting: it serves its application shell from `/` and every reference in that
shell is *relative* — `./src/index-*.js`, `favicon.ico`, `manifest.webmanifest`,
`./bootstrap`, the runtime's `assets/v<version>/…` and `api/`, and the
WebSocket URI it builds from `location.pathname`. Relative references resolve
against the document's *directory*, so mounting the shell at `/trilium/` puts
every one of them inside `/trilium/`, where :func:`upstream_path` strips the
prefix back off. The vault therefore owns one prefix and nothing else.

The slash is what makes that true, and it is also what makes the application
work at all: Trilium's own hashchange parser refuses any URL that does not
contain the literal ``/#root``, and the browser's back button inside the vault
does nothing without it. A slash-less mount is a page that paints, navigates,
and then ignores every history entry it pushed.

**Why not mount it at the site root.** Putting the shell at `/` would work for
the browser and cost the edge its landing page, its ``/ui/*`` pages and its
``/svc/*`` calls — one host cannot serve both from the root. Under a prefix the
two surfaces are disjoint by construction rather than by a carefully-argued
list, and the root-level names (`/api/`, `/assets/`, `/favicon.ico`) stay awm's.

**Why the prefix is a constant with tests.** The same reason `policy.py` gives
for the public door: a change to what the internet can reach should be a
reviewed diff, not a per-host environment string.

**What is deliberately not forwarded, and why.** Each of these is a route
Trilium serves that a browser must not reach through us. They are matched
against the *stripped* path, so `/trilium/etapi/…` is refused exactly as
`/etapi/…` was before the mount moved:

  - ``/etapi/`` — Trilium's own authentication is off on this deployment, and
    upstream's ETAPI guard stands down with it. Forwarding it would hand
    vault-origin JavaScript an unauthenticated API to the shared vault. The
    trilium service reaches it over loopback, from inside, which is what makes
    *its* use of the same surface safe.
  - ``/custom/`` — dispatches user-authored request handlers, i.e. backend
    scripting, which the child is started with disabled.
  - ``/mcp`` — always wants an ETAPI token, is not a browser path, and is a
    name awm will want for itself.
  - ``/share/`` — Trilium's public-share feature is unauthenticated by design.
    Behind the edge session it is neither public nor a second useful read path,
    so it stays off until it means something.
  - ``/login``, ``/logout``, ``/setup``, ``/set-password`` — dead with Trilium's
    own login. ``/logout`` is claimed by the edge instead, so the vault's own
    logout button ends the awm session rather than a session nobody has.

``/robots.txt`` used to be on that list and no longer needs to be: the vault
owned the site root then, so the site's own robots file was reachable through
it. Under a prefix the root is not the vault's to answer at all.

The list is a refusal on top of an allow-list rather than a deny-list on its
own: the prefix is the allow-list, and it is closed by construction.
"""

from __future__ import annotations

#: Where the application shell is served, and the mount for everything under
#: it. The trailing slash is deliberate — see the module docstring.
PREFIX = "/trilium/"

#: Where the application shell is served. The same string: under a prefix
#: mount the shell *is* the directory.
SHELL = PREFIX

#: Answered with a permanent redirect to :data:`SHELL`. A person types
#: ``/trilium``; every relative reference in the shell would then resolve one
#: level too high.
SHELL_BARE = PREFIX.rstrip("/")

_PREFIX_BYTES = PREFIX.encode("ascii")

#: Served by the edge, never proxied — see :func:`manifest`.
MANIFEST = PREFIX + "manifest.webmanifest"

#: Claimed by the edge — see ``proxy._vault_logout``.
LOGOUT = PREFIX + "logout"

#: Routes Trilium serves that we deliberately do not forward, with the reason,
#: named by their path *inside* the mount. Recorded rather than merely absent
#: so the next reader sees a decision instead of an oversight, and so a test
#: can assert each one stays unreachable.
NOT_FORWARDED = {
    "/etapi/": "unauthenticated once Trilium's own login is off",
    "/custom/": "dispatches user-authored backend scripts",
    "/mcp": "ETAPI-token only, and a name awm wants",
    "/share/": "public by design upstream; meaningless behind the session",
    "/login": "dead with Trilium's own login",
    "/logout": "claimed by the edge, so it ends the awm session",
    "/setup": "dead — the vault provisions itself",
    "/set-password": "dead with Trilium's own login",
}


def _refused(inner: str) -> bool:
    """Whether a path *inside* the mount is one we do not forward."""
    for entry in NOT_FORWARDED:
        if entry.endswith("/"):
            if inner.startswith(entry):
                return True
        elif inner == entry or inner.startswith(entry + "/"):
            return True
    return False


def owns(path: str) -> bool:
    """Whether ``path`` is served by the vault rather than by the gateway."""
    if path == SHELL_BARE:
        return True
    if not path.startswith(PREFIX):
        return False
    return not _refused(upstream_path(path))


def upstream_path(path: str) -> str:
    """The path to ask the vault for: ``path`` with the mount taken off.

    The single rewrite in the whole design. ``/trilium/`` is Trilium's ``/``,
    ``/trilium/api/tree`` is its ``/api/tree``. Keeping it in one place is what
    lets the HTTP leg, the WebSocket leg and the refusal list above all agree
    on what "inside the vault" means.
    """
    if path.startswith(PREFIX):
        return path[len(PREFIX) - 1:]
    return path


def upstream_raw_path(raw: bytes) -> bytes | None:
    """:func:`upstream_path` on the bytes as they arrived, or ``None``.

    The edge routes on the *decoded* path and forwards the *raw* one, so the
    mount has to be present in both — a target whose prefix only appears after
    percent-decoding (``/%74rilium/…``) classifies as the vault's and would be
    forwarded with the prefix still attached. ``None`` says "route said yes,
    bytes say no", which the caller answers with a 404 like any other path off
    the list.
    """
    if raw == _PREFIX_BYTES.rstrip(b"/"):
        return b"/"
    if not raw.startswith(_PREFIX_BYTES):
        return None
    return raw[len(_PREFIX_BYTES) - 1:]


def manifest() -> dict:
    """The PWA manifest, synthesized rather than proxied.

    Upstream's declares ``start_url`` and ``scope`` of ``/``, so a vault
    installed as an app from ``/trilium/`` would launch into awm's landing page
    and then treat every awm path as in-scope. Only two fields have to differ,
    and generating them here is less machinery than rewriting a proxied JSON
    body.

    ``icon.png`` is relative to this document, whose directory is the mount, so
    it resolves to ``/trilium/icon.png``, which the vault serves.
    """
    return {
        "name": "Trilium",
        "short_name": "Trilium",
        "start_url": SHELL,
        "scope": SHELL,
        "display": "standalone",
        "icons": [{"src": "icon.png", "sizes": "512x512", "type": "image/png"}],
    }

"""Which paths belong to the shared knowledge base, and where its shell lives.

The vault is the one upstream on this edge that is not the gateway. There is
exactly one of it, shared by everyone who can sign in, so this module answers
only "does this path belong to the vault?" — never "whose vault?", because that
question has no answer here and no caller can pose it.

**Why a path list at all, rather than a prefix.** Trilium has no URL-base
setting: it serves its application shell from `/` and every asset reference in
that shell is *relative*, so the assets are requested at the URL root whatever
path the shell came from. The root-level surface therefore belongs to the vault
in any design. What we get to choose is where the *shell* is, and putting it at
`/vault` rather than `/` is what lets the mesh edge keep its landing page with
no second listener and no dedicated port.

**Why `/vault` and never `/vault/`.** Relative references resolve against the
document's directory. From `/vault` that directory is `/`, so `./src/index.js`
becomes `/src/index.js` — exactly what a shell at `/` would ask for. From
`/vault/` it becomes `/vault/src/index.js`, which nothing serves. The shell
paints, the assets 404, and the page hangs half-built. Hence the redirect.

**Why the list is a constant with tests.** The same reason `policy.py` gives
for the public door: a change to what the internet can reach should be a
reviewed diff, not a per-host environment string. And the property worth
testing is not "these are Trilium's paths" but "these do not collide with
awm's" — a question only the edge can ask, because only here are both lists in
scope at once.

**What is deliberately not forwarded, and why.** Each of these is a route
Trilium serves that a browser must not reach through us:

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
  - ``/robots.txt`` — belongs to whoever owns the site, not to the vault.

The list is an allow-list rather than a deny-list because the surface cannot be
closed by enumeration in principle: upstream also mounts a static directory at
the URL root, which is empty in the builds we ship but need not stay that way.
Deny-by-default is the only direction that stays correct on its own.
"""

from __future__ import annotations

#: Where the application shell is served. Exactly this, no trailing slash.
SHELL = "/vault"

#: Answered with a permanent redirect to :data:`SHELL`. See the module
#: docstring: a trailing slash breaks every relative asset reference.
SHELL_SLASH = "/vault/"

#: Root-level paths the vault owns outright.
VAULT_EXACT = frozenset({
    SHELL,
    "/bootstrap",     # the client's own init fetch, relative to the document
    "/favicon.ico",
    "/icon.png",
})

#: Root-level prefixes the vault owns. `/assets/` covers the versioned
#: `assets/v<version>/…` fragment upstream builds its asset URLs from.
VAULT_PREFIXES = (
    "/api/",
    "/assets/",
    "/src/",
    "/node_modules/",
    "/pdfjs/",
)

#: Served by the edge, never proxied — see :func:`manifest`.
MANIFEST = "/manifest.webmanifest"

#: Routes Trilium serves that we deliberately do not forward, with the reason.
#: Recorded rather than merely absent so the next reader sees a decision instead
#: of an oversight, and so a test can assert each one stays unreachable.
NOT_FORWARDED = {
    "/etapi/": "unauthenticated once Trilium's own login is off",
    "/custom/": "dispatches user-authored backend scripts",
    "/mcp": "ETAPI-token only, and a name awm wants",
    "/share/": "public by design upstream; meaningless behind the session",
    "/login": "dead with Trilium's own login",
    "/logout": "claimed by the edge, so it ends the awm session",
    "/setup": "dead — the vault provisions itself",
    "/set-password": "dead with Trilium's own login",
    "/robots.txt": "belongs to the site, not the vault",
}


def owns(path: str) -> bool:
    """Whether ``path`` is served by the vault rather than by the gateway."""
    if path in VAULT_EXACT:
        return True
    return path.startswith(VAULT_PREFIXES)


def upstream_path(path: str) -> str:
    """The path to ask the vault for.

    The single rewrite in the whole design: the shell lives at ``/vault`` for
    us and at ``/`` for Trilium. Everything else is carried through unchanged,
    which is what keeps the relative-asset arithmetic in the browser correct.
    """
    return "/" if path == SHELL else path


def manifest() -> dict:
    """The PWA manifest, synthesized rather than proxied.

    Upstream's declares ``start_url`` and ``scope`` of ``/``, so a vault
    installed as an app from ``/vault`` would launch into awm's landing page and
    look broken. Only two fields have to differ, and generating them here is
    less machinery than rewriting a proxied JSON body.

    ``icon.png`` is relative to this document at the site root, so it resolves
    to ``/icon.png``, which the vault serves.
    """
    return {
        "name": "Vault",
        "short_name": "Vault",
        "start_url": SHELL,
        "scope": "/",
        "display": "standalone",
        "icons": [{"src": "icon.png", "sizes": "512x512", "type": "image/png"}],
    }

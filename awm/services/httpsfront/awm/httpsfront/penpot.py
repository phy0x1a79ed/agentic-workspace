"""Which paths belong to Penpot's own frontend, and where its shell lives.

Penpot is the second whole external app wired onto this edge exactly the way
:mod:`awm.httpsfront.vault` wires in Trilium: no URL-base setting of its own,
a shell whose asset references are all *relative to the document*, so they
resolve at the URL root no matter what path served the shell. Read
``vault``'s own module docstring for the mechanics that follow from that —
they are identical here down to the trailing-slash redirect, so this module
states only what is Penpot-specific.

**Where the prefixes below come from.** Read off the Penpot fork's own build
and nginx config (``projects/penpot/dev``), not guessed: the built
``index.html`` references ``css/…``, ``js/…`` and ``images/…`` relative to the
document (``docker/images/bundle-frontend/index.html``), its own
``nginx.conf.template`` proxies ``/api`` and ``/assets`` straight to the
backend and ``/plugins`` to a static dir of Penpot's bundled first-party
plugins, and the realtime collaboration socket is ``/ws/notifications``.
Client-side routing is hash-based — Penpot has no path-based SPA route at
all, and that same nginx config 301s any other multi-segment path straight to
a 404 — so this fixed set is everything a browser ever asks the origin for.

**Known collision with the vault, not resolved here.** ``/api/`` and
``/assets/`` are also root-level entries in
:data:`awm.httpsfront.vault.VAULT_PREFIXES` — both apps chose the same
conventional names for the same reason: they *are* conventional. A deployment
that runs both ``AWM_EDGE_VAULT=1`` and ``AWM_EDGE_PENPOT=1`` on one edge
therefore has two apps claiming the same root-level paths, and something has
to give: ``proxy.py`` checks the vault first, unchanged from before this
module existed, so Penpot's own ``/api``/``/assets`` traffic is silently
swallowed by Trilium on such a host. The only real fix is what the
public-sirius integrator's branch has already done for Trilium — give one app
a URL-base rather than root ownership — and that is out of scope here. Flagged
explicitly in the T12 report rather than papered over.
"""

from __future__ import annotations

#: Where the application shell is served. Exactly this, no trailing slash —
#: see ``vault.SHELL``'s docstring for why a trailing slash would break every
#: relative asset reference.
SHELL = "/penpot"

#: Answered with a permanent redirect to :data:`SHELL`, for the same reason
#: as ``vault.SHELL_SLASH``.
SHELL_SLASH = "/penpot/"

#: Root-level paths Penpot owns outright.
PENPOT_EXACT = frozenset({SHELL})

#: Root-level prefixes Penpot owns. ``/api/`` and ``/assets/`` collide with
#: the vault's own reserved names — see the module docstring.
PENPOT_PREFIXES = (
    "/js/",
    "/css/",
    "/images/",
    "/fonts/",
    "/plugins/",
    "/assets/",
    "/api/",
    "/ws/notifications",
)


def owns(path: str) -> bool:
    """Whether ``path`` is served by Penpot rather than by the gateway."""
    if path in PENPOT_EXACT:
        return True
    return path.startswith(PENPOT_PREFIXES)


def upstream_path(path: str) -> str:
    """The path to ask Penpot for.

    The single rewrite in the whole design, mirroring ``vault.upstream_path``:
    the shell lives at ``/penpot`` for us and at ``/`` for Penpot's own nginx.
    Everything else is carried through unchanged, which is what keeps the
    relative-asset arithmetic in the browser correct.
    """
    return "/" if path == SHELL else path

"""Which paths belong to Penpot's own frontend, and where its shell lives.

Penpot is the second whole external app wired onto this edge, and it mounts the
same way :mod:`awm.httpsfront.vault` mounts Trilium: one prefix, a trailing
slash that is load-bearing, and :func:`upstream_path` stripping the prefix back
off. Read ``vault``'s module docstring for the mechanics. This module states
only what is Penpot-specific, which is the one thing Penpot has and Trilium
does not — a URL-base setting of its own that has to agree with the mount.

**PENPOT_PUBLIC_URI is the other half of this file.** Penpot's client checks its
own location before it routes anything: ``on-navigate`` in
``frontend/src/app/main/ui/routes.cljs`` compares ``location.origin +
location.pathname`` against ``cf/public-uri`` by exact string equality, and a
mismatch renders the not-found page — which embeds a login dialog, so the
symptom reads as an auth failure and is not one. ``cf/public-uri`` is read at
*runtime* from the ``penpotPublicURI`` JS global, which the frontend
container's ``nginx-entrypoint.sh`` writes into ``js/config.js`` from the
``PENPOT_PUBLIC_URI`` environment variable, falling back to
``location.origin``. So the deployment contract is:

    PENPOT_PUBLIC_URI = <edge origin> + "/penpot"

with **no** trailing slash in the variable — Penpot's backend concatenates it
raw into email templates, and a doubled slash there is visible to users.
Penpot normalises it to end in ``/`` before the comparison, which is why the
browser has to be at ``/penpot/`` and why :data:`SHELL_BARE` redirects.

A mount whose prefix and ``PENPOT_PUBLIC_URI`` disagree fails as a 404 page on
every route, including the shell. There is no partial-failure mode to notice
in staging, which is the argument for the constant below over a per-host
string.

**Stripping is mandatory, not an optimisation.** Penpot's own
``nginx.conf.template`` 301s any unmatched two-segment path to ``/404``, and
its ``/api``, ``/assets`` and ``/ws/notifications`` locations are absolute. An
unstripped ``/penpot/api/…`` matches none of them and redirects instead of
reaching the backend.

**The exporter renders against a different origin.** Penpot's exporter drives a
headless browser at its own render page, and behind an authenticating edge that
browser cannot load the public origin — it has no session, so the page never
reaches network idle and every export times out. The fork carries
``PENPOT_INTERNAL_URI`` (exporter-side, upstream #10630) for exactly this: point
it at a second frontend container with ``PENPOT_PUBLIC_URI`` unset, whose config
then falls back to ``location.origin`` and whose own location check passes on
the internal address. ``replace-internal-uris`` rewrites that origin back to the
public one in the emitted SVG, so nothing internal leaks into what a caller
gets. None of that is this module's business — it is recorded here because this
docstring is where the next reader comes looking for "why did my export break
when I set the public URI".

**Nothing is refused inside the mount.** The vault carries a ``NOT_FORWARDED``
list because Trilium's own authentication is off on this deployment, which
leaves its ETAPI open to anything vault-origin JavaScript asks for. Penpot's is
on: it owns its accounts and teams and checks its own session on every backend
call, so there is no equivalent surface to close. The prefix is the whole
allow-list.
"""

from __future__ import annotations

#: Where the application shell is served, and the mount for everything under
#: it. The trailing slash is deliberate — see the module docstring.
PREFIX = "/penpot/"

#: Where the application shell is served. The same string: under a prefix
#: mount the shell *is* the directory.
SHELL = PREFIX

#: Answered with a permanent redirect to :data:`SHELL`. A person types
#: ``/penpot``; every relative reference in the shell would then resolve one
#: level too high, and Penpot's own location check would fail on the missing
#: slash regardless.
SHELL_BARE = PREFIX.rstrip("/")

_PREFIX_BYTES = PREFIX.encode("ascii")


def owns(path: str) -> bool:
    """Whether ``path`` is served by Penpot rather than by the gateway."""
    return path == SHELL_BARE or path.startswith(PREFIX)


def upstream_path(path: str) -> str:
    """The path to ask Penpot for: ``path`` with the mount taken off.

    The single rewrite in the whole design. ``/penpot/`` is Penpot's ``/``,
    ``/penpot/api/rpc/command/get-profile`` is its
    ``/api/rpc/command/get-profile``. Keeping it in one place is what lets the
    HTTP leg and the WebSocket leg agree on what "inside Penpot" means.
    """
    if path.startswith(PREFIX):
        return path[len(PREFIX) - 1:]
    return path


def upstream_raw_path(raw: bytes) -> bytes | None:
    """:func:`upstream_path` on the bytes as they arrived, or ``None``.

    The edge routes on the *decoded* path and forwards the *raw* one, so the
    mount has to be present in both — a target whose prefix only appears after
    percent-decoding (``/%70enpot/…``) classifies as Penpot's and would be
    forwarded with the prefix still attached. ``None`` says "route said yes,
    bytes say no", which the caller answers with a 404. Identical to
    ``vault.upstream_raw_path`` and load-bearing for the same reason.
    """
    if raw == _PREFIX_BYTES.rstrip(b"/"):
        return b"/"
    if not raw.startswith(_PREFIX_BYTES):
        return None
    return raw[len(_PREFIX_BYTES) - 1:]

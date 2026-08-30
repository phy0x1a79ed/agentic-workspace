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

**Penpot's own credential surface is refused.** Penpot checks its own session
on every backend call, so for a long time the prefix was the whole allow-list
here. It is not any more: awm now holds a Penpot credential per person, signs
them in with it, and replaces it nightly, so Penpot's own password commands are
no longer a second way in — they are a way to break the first one. A person who
changed their Penpot password would desynchronise the credential awm rotates on
their behalf and wedge their own account, with no HTTP path back; a person who
registered a second profile would have an identity awm knows nothing about; and
a recovery mail is a route to both, on a deployment whose only mail sink is a
container nobody reads.

Refusing them here rather than with Penpot's ``disable-login-with-password``
flag is not a preference. The exporter authenticates by cookie and takes no
access token, so turning password login off inside Penpot would leave the render
service with no way in and blank every diagram. One layer out costs nothing: the
edge and ``penpot-view`` both reach these commands on loopback, never through
this door.

:data:`NOT_FORWARDED` therefore lists *commands*, not paths — see the two
prefixes above it, which is the part that is easy to get wrong.
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

#: The cookie Penpot's backend sets and its exporter accepts, at ``path=/`` and
#: ``HttpOnly``. Named here because the edge both sets it (on behalf of a
#: signed-in person) and clears it (when the awm identity changes), and both
#: have to agree with what Penpot itself uses or the browser ends up holding
#: two. Penpot allows it to be renamed via ``PENPOT_AUTH_TOKEN_COOKIE_NAME``;
#: this deployment does not, and a deployment that did would have to say so
#: here as well.
COOKIE_NAME = "auth-token"


#: Where Penpot dispatches a named RPC command, *inside* the mount. Two, not
#: one: upstream routes ``/api/rpc/command/:method-name`` (what the frontend
#: calls) and ``/api/main/methods/:method-name`` (the documented API) into the
#: same method map, so a refusal that closed only the first would leave every
#: command below reachable under the second. Anything added to Penpot's route
#: table that reaches ``methods`` belongs here too.
RPC_PREFIXES = ("/api/rpc/command/", "/api/main/methods/")

#: Penpot commands the edge does not forward, with the reason. Recorded rather
#: than merely absent so the next reader sees a decision, and so a test can
#: assert each one stays unreachable. See the module docstring for why this
#: list exists at all and why it is not ``disable-login-with-password``.
NOT_FORWARDED = {
    "login-with-password": "awm signs people in; a second credential is the "
                           "thing this deployment removed",
    "register-profile": "an identity awm holds no credential for",
    "prepare-register-profile": "the first half of the same",
    "request-profile-recovery": "mail goes to a container nobody reads",
    "recover-profile": "the second half of the same",
    "update-profile-password": "desynchronises the credential awm rotates, "
                               "with no HTTP path back",
    "request-email-change": "the stored credential is keyed by the email; "
                            "changing it wedges the same way",
}


def refused(path: str) -> bool:
    """Whether ``path`` names one of Penpot's own credential commands.

    Case-folded because a refusal that a different capitalisation walks past is
    not a refusal. Penpot's own routes are case-sensitive, so folding here can
    only ever refuse *more* than Penpot would answer.
    """
    if not path.startswith(PREFIX):
        return False
    inner = upstream_path(path).casefold()
    for prefix in RPC_PREFIXES:
        if inner.startswith(prefix):
            return inner[len(prefix):].split("/", 1)[0] in NOT_FORWARDED
    return False


def owns(path: str) -> bool:
    """Whether ``path`` is served by Penpot rather than by the gateway."""
    if path == SHELL_BARE:
        return True
    if not path.startswith(PREFIX):
        return False
    return not refused(path)


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

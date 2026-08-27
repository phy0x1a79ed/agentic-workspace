"""What Penpot owns at the URL root, and the public door that has to name it
explicitly.

Two failure classes this pins, each with its own prevented-bug docstring on
the test: (1) ``owns()``/the shell rewrite silently drifting from what
Penpot's real fork and nginx config actually serve, and (2) the public
``OPEN_PREFIXES`` list missing ``/penpot`` or ``/penpot-view`` — which, per
the public-sirius integrator's branch, 404s the entire feature from the
internet even though every other check in this module would have allowed it.
"""

from __future__ import annotations

import pytest

from awm.httpsfront import penpot, policy, vault

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

#: Root-level paths Penpot's real nginx config and built frontend actually
#: reference — read off projects/penpot/dev, not guessed. Excludes
#: `/assets/` and `/api/`, which `owns()` also claims but which `classify()`
#: resolves to the vault instead — see `test_the_known_vault_collision`.
PENPOT_PATHS = [
    "/penpot",
    "/js/main.js",
    "/js/config.js",
    "/css/main.css",
    "/images/favicon.png",
    "/fonts/some-font.woff2",
    "/plugins/create-palette-plugin/manifest.json",
    "/ws/notifications",
]

#: The two prefixes Penpot's real nginx config also needs that collide with
#: the vault's own reserved root-level names. `owns()` claims them (Penpot
#: cannot function without them when it is the only app on the edge); the
#: *routing* precedence that resolves the collision belongs to proxy.py and
#: policy.classify(), documented and pinned separately below.
PENPOT_COLLIDING_PATHS = ["/assets/by-id/abc123", "/api/rpc/command/get-profile"]

#: Prefixes awm serves on the same origin. A Penpot path that fell inside any
#: of these — or a future awm mount that fell inside a Penpot prefix — would
#: shadow the other silently.
AWM_PREFIXES = [
    "/ui/", "/svc/", "/files/", "/__auth/", "/__landing/", "/drawio-app/",
    "/hub/", "/invoke", "/tools", "/ca.crt", "/ca.pem", "/robots.txt",
]


@pytest.mark.parametrize("path", PENPOT_PATHS)
def test_penpot_owns_its_root_level_paths(path):
    """Prevents: a root-level path Penpot's own frontend actually requests
    (read off its build and nginx config) silently falling through to the
    gateway instead of Penpot — a blank page or a 404 with no proxy error."""
    assert penpot.owns(path), path
    assert policy.classify(path) is policy.Verdict.PENPOT


@pytest.mark.parametrize("path", PENPOT_COLLIDING_PATHS)
def test_the_known_vault_collision(path):
    """Documents, rather than hides, a real limitation: `/assets/` and
    `/api/` are claimed by both `penpot.owns()` and `vault.VAULT_PREFIXES`.
    `classify()` resolves it in the vault's favour (checked first, so the
    pre-existing feature's behaviour does not shift under this change) — which
    means Penpot's own `/assets`/`/api` traffic is silently swallowed by
    Trilium on any host running both. If this test ever starts failing
    because `owns()` stopped claiming these paths, that is a *regression* in
    Penpot's own functionality, not a fix for the collision — the two must be
    told apart."""
    assert penpot.owns(path)
    assert vault.owns(path)
    assert policy.classify(path) is policy.Verdict.VAULT


@pytest.mark.parametrize("path", AWM_PREFIXES)
def test_penpot_and_awm_do_not_overlap(path):
    """Prevents: Penpot's root-level ownership shadowing an awm mount, or
    vice versa — containment in both directions, not mere inequality, because
    a near-miss (`/api` against `/ap`, a future awm page under `/assets/`) is
    the dangerous shape a set-inequality check would miss."""
    owned = sorted(penpot.PENPOT_EXACT) + list(penpot.PENPOT_PREFIXES)
    for p in owned:
        assert not p.startswith(path), f"penpot path {p} falls inside awm's {path}"
        assert not path.startswith(p), f"awm path {path} falls inside penpot's {p}"


def test_root_is_never_penpots():
    """Prevents: `/` — the one path the sign-in form reloads — being
    hijacked by Penpot's shell rewrite, which would proxy the login page's
    own reload straight into Penpot instead of back to the login form."""
    assert not penpot.owns("/")
    assert policy.classify("/") is policy.Verdict.OPEN


def test_the_shell_is_rewritten_and_nothing_else_is():
    """Prevents: a rewrite rule broadening past the shell itself and mangling
    a real asset path (e.g. turning `/js/main.js` into `/main.js`, which
    Penpot's nginx does not serve)."""
    assert penpot.upstream_path(penpot.SHELL) == "/"
    for path in PENPOT_PATHS:
        if path != penpot.SHELL:
            assert penpot.upstream_path(path) == path


def test_a_trailing_slash_is_not_the_shell():
    """Prevents: `/penpot/` serving the shell — Penpot's relative asset
    references would then resolve one directory too deep (`/penpot/js/...`,
    which nothing serves) and the page would paint and then hang."""
    assert not penpot.owns(penpot.SHELL_SLASH)


def test_a_peer_bearer_is_not_a_person():
    """Prevents: a machine bearer (another node's process, or the shared
    operator session) reaching a person's Penpot design files — the same
    exclusion vault.py enforces for Trilium, and for the same reason."""
    assert policy.allows(penpot.SHELL, "tony")
    assert not policy.allows(penpot.SHELL, "peer")
    assert not policy.allows(penpot.SHELL, "operator")
    assert not policy.allows(penpot.SHELL, None)


# -- the public door -----------------------------------------------------
#
# Prevents the single failure this task exists to guard against: on the
# public-sirius integrator's branch, OPEN_PREFIXES is the *only* gate — an
# unlisted prefix is a 404 regardless of anything classify() would otherwise
# say. A cheap membership check, but its absence takes the whole feature down
# invisibly (every other test in this file would still pass).

def test_penpot_shell_is_in_open_prefixes():
    assert "/penpot" in policy.OPEN_PREFIXES


def test_penpot_view_is_in_open_prefixes():
    assert "/penpot-view" in policy.OPEN_PREFIXES


@pytest.mark.parametrize("path", ["/penpot", "/penpot/anything",
                                  "/penpot-view/f/p/b"])
def test_open_prefixes_actually_cover_the_real_urls(path):
    """Prevents: the listed prefix strings existing but not actually
    matching the URLs a browser sends (a trailing-slash or spelling mismatch
    would pass a bare membership test while still 404ing every real request).
    """
    assert path.startswith(tuple(policy.OPEN_PREFIXES))

"""What the vault owns at the URL root, and that it collides with nothing.

The interesting property is not "these are Trilium's paths" — that is upstream's
business and changes when upstream changes. It is that the vault's surface and
awm's own are *disjoint*, which is what makes one origin possible at all. Only
the edge can assert it, because only here are both lists in scope.
"""

from __future__ import annotations

import pytest

from awm.httpsfront import policy, vault

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

#: Every root-level path Trilium serves that a browser needs from us.
VAULT_PATHS = [
    "/vault",
    "/bootstrap",
    "/favicon.ico",
    "/icon.png",
    "/api/tree",
    "/api/notes/abc123",
    "/assets/v0.105.0/src/index.js",
    "/assets/index-abc.js",
    "/src/index-Dso57yoQ.js",
    "/node_modules/@excalidraw/excalidraw/dist/prod/x.js",
    "/pdfjs/viewer.html",
]

#: Prefixes awm serves on the same origin. A vault path that fell inside any of
#: these — or a future awm mount that fell inside a vault prefix — would shadow
#: the other silently, which is the failure this module exists to prevent.
AWM_PREFIXES = [
    "/ui/", "/svc/", "/files/", "/__auth/", "/__landing/", "/drawio-app/",
    "/hub/", "/invoke", "/tools", "/ca.crt", "/ca.pem", "/robots.txt",
]


@pytest.mark.parametrize("path", VAULT_PATHS)
def test_the_vault_owns_its_root_level_paths(path):
    assert vault.owns(path), path
    assert policy.classify(path) is policy.Verdict.VAULT


@pytest.mark.parametrize("path", sorted(vault.NOT_FORWARDED))
def test_deliberately_unforwarded_paths_are_not_the_vaults(path):
    """Each of these is a live Trilium route we refuse to proxy.

    They are listed rather than merely absent so this test can exist: an
    allow-list that records only what is open cannot fail when something is
    quietly re-opened.
    """
    assert not vault.owns(path), path
    assert not vault.owns(path.rstrip("/") + "/x")
    assert policy.classify(path) is policy.Verdict.DENY


def test_the_vault_and_awm_do_not_overlap():
    """Containment in both directions, not set equality.

    A near-miss is the dangerous shape: `/api` against `/ap`, or a future awm
    page mounted at `/assets/`. Checking only that the two lists differ would
    pass on both.
    """
    owned = sorted(vault.VAULT_EXACT) + list(vault.VAULT_PREFIXES)
    for v in owned:
        for a in AWM_PREFIXES:
            assert not v.startswith(a), f"vault path {v} falls inside awm's {a}"
            assert not a.startswith(v), f"awm path {a} falls inside vault's {v}"


def test_root_is_never_the_vaults():
    """`/` belongs to whoever is hosting, on every profile.

    This is what lets a mesh edge keep its landing page while serving the vault
    at /vault — the whole reason the shell is not mounted at the root.
    """
    assert not vault.owns("/")
    assert policy.classify("/") is policy.Verdict.OPEN


def test_the_shell_is_rewritten_and_nothing_else_is():
    assert vault.upstream_path(vault.SHELL) == "/"
    for path in VAULT_PATHS:
        if path != vault.SHELL:
            assert vault.upstream_path(path) == path


def test_a_trailing_slash_is_not_the_shell():
    """`/vault/` must never serve the shell.

    Relative asset references resolve against the document's directory: from
    `/vault` that is the site root and they are found, from `/vault/` they are
    requested one level deep and none of them exist. The page paints and hangs.
    """
    assert not vault.owns(vault.SHELL_SLASH)


def test_the_manifest_launches_the_vault_not_the_host():
    """Upstream's manifest says `start_url: "/"`, which would launch an
    installed vault into awm's landing page."""
    m = vault.manifest()
    assert m["start_url"] == vault.SHELL
    assert m["scope"] == "/"


def test_a_peer_bearer_is_not_a_person():
    """The vault is a human's knowledge base; a peer is another node's process."""
    assert policy.allows("/vault", "tony")
    assert not policy.allows("/vault", "peer")
    assert not policy.allows("/vault", "operator")
    assert not policy.allows("/vault", None)

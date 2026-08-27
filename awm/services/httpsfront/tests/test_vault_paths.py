"""What the vault owns, and that it collides with nothing.

The interesting property is not "these are Trilium's paths" — that is upstream's
business and changes when upstream changes. It is that the vault's surface and
awm's own are *disjoint*, which is what makes one origin possible at all. Only
the edge can assert it, because only here are both lists in scope.

Under a prefix mount that property is nearly free, which is most of why the
mount moved. What is not free is the refusal list: everything below the mount is
the vault's by default, so each route we decline to forward has to be declined
against the *stripped* path, and this is the module that pins it.
"""

from __future__ import annotations

import pytest

from awm.httpsfront import policy, vault

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

#: Every path Trilium serves that a browser needs from us, under the mount.
#: They are relative references in the shell, so the browser asks for them at
#: exactly these addresses once the shell's directory is the mount.
VAULT_PATHS = [
    vault.SHELL,
    vault.SHELL + "bootstrap",
    vault.SHELL + "favicon.ico",
    vault.SHELL + "icon.png",
    vault.SHELL + "api/tree",
    vault.SHELL + "api/notes/abc123",
    vault.SHELL + "assets/v0.105.0/src/index.js",
    vault.SHELL + "assets/index-abc.js",
    vault.SHELL + "src/index-Dso57yoQ.js",
    vault.SHELL + "node_modules/@excalidraw/excalidraw/dist/prod/x.js",
    vault.SHELL + "pdfjs/viewer.html",
]

#: Prefixes awm serves on the same origin. A vault path that fell inside any of
#: these — or a future awm mount that fell inside the vault's — would shadow the
#: other silently, which is the failure this module exists to prevent.
AWM_PREFIXES = [
    "/ui/", "/svc/", "/files/", "/__auth/", "/__landing/", "/drawio-app/",
    "/hub/", "/invoke", "/tools", "/ca.crt", "/ca.pem", "/robots.txt",
    "/api/", "/assets/", "/src/", "/favicon.ico", "/bootstrap",
]


@pytest.mark.parametrize("path", VAULT_PATHS)
def test_the_vault_owns_everything_under_its_mount(path):
    assert vault.owns(path), path
    assert policy.classify(path) is policy.Verdict.VAULT


@pytest.mark.parametrize("inner", sorted(vault.NOT_FORWARDED))
def test_deliberately_unforwarded_routes_are_not_the_vaults(inner):
    """Each of these is a live Trilium route we refuse to proxy.

    Under a prefix mount this is the *only* thing standing between a browser and
    them: everything below the mount is otherwise the vault's. So the assertion
    is on the mounted address, which is what a browser would actually ask for.
    """
    path = vault.SHELL + inner.lstrip("/")
    assert not vault.owns(path), path
    assert not vault.owns(path.rstrip("/") + "/x")
    assert policy.classify(path) is policy.Verdict.DENY


def test_the_root_level_names_are_awms_again():
    """The mount is what gives them back.

    While the shell sat at a slash-less path the vault owned `/api/`, `/src/`,
    `/assets/` and `/favicon.ico` at the site root, because its relative
    references resolved there. A mesh node's own surface had to be argued around
    that; now it simply is not the vault's.
    """
    for path in ("/api/tree", "/src/index.js", "/assets/x.css", "/favicon.ico",
                 "/bootstrap", "/icon.png", "/robots.txt"):
        assert not vault.owns(path), path


def test_the_vault_and_awm_do_not_overlap():
    """Containment in both directions, not set equality.

    A near-miss is the dangerous shape: `/trilium/` against `/ui/trilium`, or a
    future awm page mounted inside the vault. Checking only that the two differ
    would pass on both.
    """
    for a in AWM_PREFIXES:
        assert not vault.PREFIX.startswith(a), f"the vault falls inside awm's {a}"
        assert not a.startswith(vault.PREFIX), f"awm path {a} falls inside the vault"


def test_the_management_page_is_not_the_application():
    """`/ui/trilium` renders awm's own controls; `/trilium/` is Trilium itself.

    Two things named for the same service on one origin, and only the leading
    segment tells them apart — so assert it rather than trust the eye.
    """
    assert not vault.owns("/ui/trilium")
    assert not vault.owns("/ui/trilium/")
    assert policy.classify("/ui/trilium") is policy.Verdict.OPEN


def test_root_is_never_the_vaults():
    """`/` belongs to whoever is hosting, on every profile.

    This is what lets a mesh edge keep its landing page while serving the vault
    under a prefix — the whole reason the shell is not mounted at the root.
    """
    assert not vault.owns("/")
    assert policy.classify("/") is policy.Verdict.OPEN


def test_the_mount_is_stripped_and_nothing_else_is():
    assert vault.upstream_path(vault.SHELL) == "/"
    assert vault.upstream_path(vault.SHELL + "api/tree") == "/api/tree"
    for path in ("/", "/ui/drawio/", "/svc/trilium/fn/status"):
        assert vault.upstream_path(path) == path


def test_the_bare_name_is_owned_so_it_can_be_redirected():
    """`/trilium` must reach the edge's own redirect, not fall through to the
    gateway — and must never itself serve the shell, since every relative
    reference in it would then resolve one level too high."""
    assert vault.owns(vault.SHELL_BARE)
    assert policy.classify(vault.SHELL_BARE) is policy.Verdict.VAULT


def test_the_raw_mount_must_be_in_the_bytes_too():
    """Routing reads the decoded path; forwarding sends the raw one.

    A target whose mount only appears after percent-decoding classifies as the
    vault's, so the byte-level strip has to be able to say "not mine" rather
    than forward the prefix along with the request.
    """
    assert vault.upstream_raw_path(b"/trilium/api/search/%23x") == b"/api/search/%23x"
    assert vault.upstream_raw_path(b"/trilium/") == b"/"
    assert vault.upstream_raw_path(b"/trilium") == b"/"
    assert vault.upstream_raw_path(b"/%74rilium/api/tree") is None


def test_the_manifest_launches_the_vault_not_the_host():
    """Upstream's manifest says `start_url: "/"`, which would launch an
    installed vault into awm's landing page and put every awm path in scope."""
    m = vault.manifest()
    assert m["start_url"] == vault.SHELL
    assert m["scope"] == vault.SHELL


def test_a_peer_bearer_is_not_a_person():
    """The vault is a human's knowledge base; a peer is another node's process."""
    assert policy.allows(vault.SHELL, "tony")
    assert not policy.allows(vault.SHELL, "peer")
    assert not policy.allows(vault.SHELL, "operator")
    assert not policy.allows(vault.SHELL, None)

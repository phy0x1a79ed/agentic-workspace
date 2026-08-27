"""What Penpot owns, and the two things that have to agree for it to work.

The interesting property is not "these are Penpot's paths" — that is upstream's
business and changes when upstream changes. It is that Penpot's surface, the
vault's and awm's own are *disjoint*, which is what lets one origin carry all
three. Only the edge can assert it, because only here are all three in scope.

The mount makes that nearly free. What is not free is the agreement between the
mount and the ``PENPOT_PUBLIC_URI`` the containers are given: Penpot compares
its own location against that value by exact string equality and renders its
not-found page on any mismatch. Nothing in this process can read the
containers' environment, so what is pinned here is the *shape* the deployment
has to produce — no trailing slash in the variable, a trailing slash in the
mount — and the compose file is checked against it in
``test_penpot_public_uri.py``.
"""

from __future__ import annotations

import pytest

from awm.httpsfront import penpot, policy, vault

pytestmark = [pytest.mark.unit, pytest.mark.smoke]

#: Every path Penpot's real nginx config and built frontend reference — read
#: off projects/penpot/dev, not guessed. They are relative references in the
#: shell, so the browser asks for them at exactly these addresses once the
#: shell's directory is the mount.
PENPOT_PATHS = [
    penpot.SHELL,
    penpot.SHELL + "js/main.js",
    penpot.SHELL + "js/config.js",
    penpot.SHELL + "css/main.css",
    penpot.SHELL + "images/favicon.png",
    penpot.SHELL + "fonts/some-font.woff2",
    penpot.SHELL + "plugins/create-palette-plugin/manifest.json",
    penpot.SHELL + "ws/notifications",
    penpot.SHELL + "api/rpc/command/get-profile",
    penpot.SHELL + "assets/by-id/abc123",
    # Penpot's own outbound proxies, declared in its nginx `location.d`
    # overrides rather than its main config, which is why they are easy to
    # miss. Missing the first is not a visible failure — the app runs and
    # every piece of text silently renders in a fallback face.
    penpot.SHELL + "internal/gfonts/css?family=Work+Sans",
    penpot.SHELL + "github/penpot-files/anything",
]

#: Prefixes awm serves on the same origin. A Penpot path that fell inside any
#: of these — or a future awm mount that fell inside Penpot's — would shadow
#: the other silently, which is the failure this module exists to prevent.
AWM_PREFIXES = [
    "/ui/", "/svc/", "/files/", "/__auth/", "/__landing/", "/drawio-app/",
    "/hub/", "/invoke", "/tools", "/ca.crt", "/ca.pem", "/robots.txt",
    "/api/", "/assets/", "/src/", "/favicon.ico", "/bootstrap",
]


@pytest.mark.parametrize("path", PENPOT_PATHS)
def test_penpot_owns_everything_under_its_mount(path):
    assert penpot.owns(path), path
    assert policy.classify(path) is policy.Verdict.PENPOT


def test_the_root_level_names_are_awms():
    """The mount is what keeps them.

    Penpot's frontend asks for `/js/`, `/css/`, `/api/`, `/assets/` and
    `/ws/notifications` relative to its document. Mounted at the site root it
    owned all of those outright, which collided with the vault and with awm's
    own surface. Under the mount they are simply not Penpot's.
    """
    for path in ("/js/main.js", "/css/main.css", "/api/rpc/command/get-profile",
                 "/assets/by-id/abc", "/ws/notifications", "/images/x.png",
                 "/fonts/x.woff2", "/plugins/x/manifest.json", "/favicon.ico"):
        assert not penpot.owns(path), path


def test_penpot_and_awm_do_not_overlap():
    """Containment in both directions, not set equality.

    A near-miss is the dangerous shape: `/penpot/` against `/ui/penpot`, or a
    future awm page mounted inside Penpot. Checking only that the two differ
    would pass on both.
    """
    for a in AWM_PREFIXES:
        assert not penpot.PREFIX.startswith(a), f"penpot falls inside awm's {a}"
        assert not a.startswith(penpot.PREFIX), f"awm path {a} falls inside penpot"


def test_penpot_and_the_vault_do_not_overlap():
    """The collision this edge used to carry, asserted gone.

    Both apps once claimed `/api/` and `/assets/` at the site root, and the
    proxy resolved it by checking the vault first — so Penpot loaded its shell
    and then had every backend call answered by Trilium. Two prefixes make the
    two surfaces disjoint by construction.
    """
    assert not vault.PREFIX.startswith(penpot.PREFIX)
    assert not penpot.PREFIX.startswith(vault.PREFIX)
    for path in PENPOT_PATHS:
        assert not vault.owns(path), path


def test_the_management_page_is_not_the_application():
    """`/ui/penpot` renders awm's own controls; `/penpot/` is Penpot itself."""
    assert not penpot.owns("/ui/penpot")
    assert not penpot.owns("/ui/penpot/")


def test_the_view_service_is_not_the_application():
    """`/penpot-view/…` is awm's render service, a different mount that merely
    starts with the same letters. A prefix test written without the trailing
    slash would swallow it whole."""
    assert not penpot.owns("/penpot-view/f/p/b")
    assert not penpot.owns("/penpot-view")


def test_root_is_never_penpots():
    """`/` belongs to whoever is hosting, on every profile.

    This is what gives the edge back its landing page and the public profile
    its home redirect — the whole reason the shell is not mounted at the root.
    """
    assert not penpot.owns("/")
    assert policy.classify("/") is policy.Verdict.OPEN


def test_the_mount_is_stripped_and_nothing_else_is():
    assert penpot.upstream_path(penpot.SHELL) == "/"
    assert penpot.upstream_path(penpot.SHELL + "js/main.js") == "/js/main.js"
    for path in ("/", "/ui/drawio/", "/svc/penpot/fn/status", "/penpot-view/x"):
        assert penpot.upstream_path(path) == path


def test_the_bare_name_is_owned_so_it_can_be_redirected():
    """`/penpot` must reach the edge's own redirect, not fall through to the
    gateway — and must never itself serve the shell: every relative reference
    would resolve one level too high, and Penpot's own location check compares
    against a value that always ends in a slash."""
    assert penpot.owns(penpot.SHELL_BARE)
    assert policy.classify(penpot.SHELL_BARE) is policy.Verdict.PENPOT


def test_the_raw_mount_must_be_in_the_bytes_too():
    """Routing reads the decoded path; forwarding sends the raw one.

    A target whose mount only appears after percent-decoding classifies as
    Penpot's, so the byte-level strip has to be able to say "not mine" rather
    than forward the prefix along with the request.
    """
    assert penpot.upstream_raw_path(b"/penpot/api/x%23y") == b"/api/x%23y"
    assert penpot.upstream_raw_path(b"/penpot/") == b"/"
    assert penpot.upstream_raw_path(b"/penpot") == b"/"
    assert penpot.upstream_raw_path(b"/%70enpot/js/main.js") is None


def test_a_peer_bearer_is_not_a_person():
    """A machine bearer — another node's process, or the shared operator
    session — has no business in a person's design files."""
    assert policy.allows(penpot.SHELL, "tony")
    assert not policy.allows(penpot.SHELL, "peer")
    assert not policy.allows(penpot.SHELL, "operator")
    assert not policy.allows(penpot.SHELL, None)


# -- the mount and PENPOT_PUBLIC_URI have to agree -------------------------


def test_the_variable_carries_no_trailing_slash():
    """Penpot's backend concatenates ``PENPOT_PUBLIC_URI`` raw into email
    templates, so a trailing slash there is a doubled slash in every link it
    sends. The client normalises the value to end in one before comparing, so
    dropping it costs nothing on the browser side."""
    assert penpot.SHELL_BARE == penpot.SHELL.rstrip("/")
    assert not penpot.SHELL_BARE.endswith("/")


def test_the_mount_carries_one():
    """The comparison is ``location.origin + location.pathname`` against a
    value normalised to end in ``/``. A shell served at the slash-less path
    fails it and renders the not-found page, which embeds a login dialog — so
    the symptom reads as a session problem and is not one."""
    assert penpot.SHELL.endswith("/")


# -- the public door -------------------------------------------------------
#
# On the public profile OPEN_PREFIXES is the only gate: an unlisted prefix is
# a 404 regardless of anything classify() would otherwise say. A cheap
# membership check, but its absence takes the whole feature down invisibly —
# every other test in this file would still pass.


def test_penpot_shell_is_in_open_prefixes():
    assert "/penpot" in policy.OPEN_PREFIXES


def test_penpot_view_is_in_open_prefixes():
    assert "/penpot-view" in policy.OPEN_PREFIXES


@pytest.mark.parametrize("path", ["/penpot", "/penpot/", "/penpot/anything",
                                  "/penpot-view/f/p/b"])
def test_open_prefixes_actually_cover_the_real_urls(path):
    """Prevents: the listed prefix strings existing but not actually matching
    the URLs a browser sends. A trailing-slash or spelling mismatch would pass
    a bare membership test while still 404ing every real request."""
    assert path.startswith(tuple(policy.OPEN_PREFIXES))


# -- the collision warning is derived, not asserted ------------------------


def test_the_overlap_is_computed_and_is_empty():
    """Whether Penpot and the vault collide is a property of their current
    claims, not a fact about them. Both now mount under a prefix, so the
    intersection is empty — and it is computed rather than declared so a future
    mount that *does* collide says so at startup instead of failing as one app
    silently swallowing the other's traffic."""
    from awm.httpsfront import hub_adapter

    overlap = hub_adapter._claimed_by_both()
    assert overlap == []

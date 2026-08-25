"""The client patches, and the one whose absence is silent.

`webapp/` is a build, not source: install.sh clones upstream and lays awm's
patches over it. That makes the patches losable in a way the service cannot
notice — nothing here runs the browser, so a webapp missing a patch passes
every other test in this suite.

Two of the three patches announce themselves when missing. Without the
`app.min.js` injection the editor saves nothing, and the install fails loudly
rather than shipping that. Without `PreConfig.js` there is no client at all.

`PostConfig.js` is the quiet one. It registers `ellipticArcEdgeStyle`, and an
unregistered edge style is not an error in mxGraph — the edge falls back to the
default router. So the failure mode is every arc in the store rendering
straight (or as a plain bezier where the style also sets `curved=1`), with
nothing logged, no failed save, and the stored document unchanged. It went
missing exactly once already, in the port from the prototype.

These tests are cheap and pin the shape of the patch rather than its behaviour;
the rendering itself only verifies in a real browser (see INSTALL.md § Verify).
"""

from __future__ import annotations

from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parent.parent
PATCHES = SERVICE / "patches"
WEBAPP = SERVICE / "webapp"

# Every patch that is a whole-file replacement, and so is re-applied on every
# install.sh run. The app.min.js injection is deliberately not here: it is a
# text edit into a minified bundle, applied only on a fresh clone.
REPLACEMENTS = ["PreConfig.js", "PostConfig.js"]


@pytest.mark.parametrize("name", REPLACEMENTS)
def test_patch_source_exists(name):
    assert (PATCHES / name).is_file(), f"patches/{name} is missing from the repo"


def test_postconfig_registers_the_arc_edge_style():
    """The registration itself — the line whose loss renders every arc straight."""
    src = (PATCHES / "PostConfig.js").read_text(encoding="utf-8")
    assert "mxStyleRegistry.putValue(" in src
    assert "'ellipticArcEdgeStyle'" in src
    assert "mxEdgeStyle.EllipticArc" in src


def test_postconfig_keeps_the_upstream_stub():
    """It replaces upstream's PostConfig.js wholesale, so it has to carry
    upstream's one line of content forward too."""
    src = (PATCHES / "PostConfig.js").read_text(encoding="utf-8")
    assert "window.ICONSEARCH_PATH = null;" in src


@pytest.mark.parametrize("name", REPLACEMENTS)
def test_installed_webapp_matches_the_patch(name):
    """A webapp built before a patch existed serves upstream's stub and looks
    entirely healthy. Re-running install.sh is the fix; this is what notices."""
    if not (WEBAPP / "index.html").is_file():
        pytest.skip("no webapp/ installed (DRAWIO_SKIP_APP) — nothing to compare")
    installed = WEBAPP / "js" / name
    assert installed.is_file(), f"webapp/js/{name} is missing — re-run install.sh"
    assert installed.read_bytes() == (PATCHES / name).read_bytes(), (
        f"webapp/js/{name} differs from patches/{name} — the installed client is "
        "stale or unpatched; re-run install.sh"
    )


def test_preconfig_remembers_the_last_seen_view_image():
    """What paints a placed view before the network answers. Without it a
    reopened consumer diagram shows empty boxes until every image round-trips —
    and since nothing here runs a browser, losing it would be silent."""
    src = (PATCHES / "PreConfig.js").read_text(encoding="utf-8")
    assert "indexedDB.open(VIEW_DB" in src
    assert "URL.createObjectURL" in src


def test_preconfig_revalidates_rather_than_trusting_the_cache():
    """A cached image is what is *shown*, never what is believed. The
    conditional has to be ours: left to the browser, `cache: 'default'` answers
    200 out of its own copy and the 304 never reaches us."""
    src = (PATCHES / "PreConfig.js").read_text(encoding="utf-8")
    assert "'If-None-Match'" in src
    assert "cache: 'no-store'" in src
    assert "r.status === 304" in src


def test_preconfig_bounds_the_view_store():
    """The query space is caller-controlled: one diagram cycled through colour
    variants would grow this without limit."""
    src = (PATCHES / "PreConfig.js").read_text(encoding="utf-8")
    assert "VIEW_MAX_ENTRIES" in src and "VIEW_MAX_BYTES" in src
    assert "function evictViews()" in src


def test_preconfig_sends_no_cache_buster():
    """`rev` is a revision selector on the server, not a spare parameter. The
    client used to bust its refreshes with `rev=<epoch-ms>`, which 404s — so the
    refresh that was supposed to fix a stale image did nothing at all, and the
    stale image stayed. `cache: 'no-store'` is what keeps the browser's own copy
    out of the way; nothing needs appending to the URL."""
    src = (PATCHES / "PreConfig.js").read_text(encoding="utf-8")
    assert "'rev='" not in src and '"rev="' not in src
    assert "Date.now()" not in src.split("function requestViewImage", 1)[1]


def test_preconfig_does_not_read_a_failed_fetch_as_unchanged():
    """Only a 304 means "unchanged". Any other failure answered as "unchanged"
    leaves the placement showing the previous render with nothing reported —
    which is how a stale picture outlives a fixed server."""
    src = (PATCHES / "PreConfig.js").read_text(encoding="utf-8")
    body = src.split("function requestViewImage(url) {", 1)[1].split("\n  }", 1)[0]
    assert "if (r.status === 304) return null;" in body
    assert "if (!r.ok) throw" in body


def test_preconfig_refetches_rather_than_joining_a_stale_inflight_request():
    """Autosave fires every two seconds. A refresh that joins the fetch already
    in flight answers with the save before last, so the newest edit never
    appears."""
    src = (PATCHES / "PreConfig.js").read_text(encoding="utf-8")
    body = src.split("function fetchViewImage(url) {", 1)[1].split("\n  }", 1)[0]
    assert "viewStale[url] = true;" in body
    assert "delete viewStale[url];" in body

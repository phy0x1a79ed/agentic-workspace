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


def test_preconfig_does_not_key_the_view_store_on_the_cache_buster():
    """`?rev=` moves on every refresh; keyed on it the store would write a fresh
    entry each time and never once hit."""
    src = (PATCHES / "PreConfig.js").read_text(encoding="utf-8")
    body = src.split("function cacheKey(url) {", 1)[1].split("\n  }", 1)[0]
    assert "rev=" in body and "replace" in body

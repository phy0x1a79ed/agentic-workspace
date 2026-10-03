"""Tests for awm.gateway.hub.discovery.services_root.

The gateway tree is the intentional nested ``awm/gateway/awm/gateway`` layout so
a worktree can shadow ``awm.gateway`` via PYTHONPATH. When more than one root
carries ``awm.gateway``, the import system resolves it as a PEP 420 *namespace*
package: ``__file__`` is ``None`` (only ``__path__`` is populated). The old
``Path(awm.gateway.__file__)`` raised ``TypeError`` in that state and wedged
service discovery / bootstrap wholesale, leaving every service stuck
``starting``. These tests pin the fix: the resolver must work under both
package resolutions.
"""

from __future__ import annotations

import pytest
pytestmark = [pytest.mark.unit, pytest.mark.smoke]

import awm.gateway
from awm.gateway.hub import discovery


def test_services_root_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_SERVICES_DIR", str(tmp_path))
    assert discovery.services_root() == tmp_path.resolve()


def test_services_root_with_file_set(monkeypatch):
    """Normal install: ``__file__`` points at the package ``__init__.py``."""
    monkeypatch.delenv("AWM_SERVICES_DIR", raising=False)
    root = discovery.services_root()
    assert root.name == "services"
    # Anchored to the gateway's own ``awm/`` parent, not cwd.
    assert root.parent.name == "awm"


def test_services_root_with_file_none(monkeypatch):
    """Namespace-package resolution: ``__file__`` is ``None``. Must fall back to
    ``__path__`` rather than raising ``TypeError`` on ``Path(None)``."""
    monkeypatch.delenv("AWM_SERVICES_DIR", raising=False)
    monkeypatch.setattr(awm.gateway, "__file__", None, raising=False)
    # Sanity: ``__path__`` is always populated for a (namespace) package.
    assert list(awm.gateway.__path__)
    root = discovery.services_root()  # must not raise
    assert root.name == "services"


def test_services_root_file_and_namespace_agree(monkeypatch):
    """Both resolutions land on the same services dir."""
    monkeypatch.delenv("AWM_SERVICES_DIR", raising=False)
    with_file = discovery.services_root()
    monkeypatch.setattr(awm.gateway, "__file__", None, raising=False)
    without_file = discovery.services_root()
    assert with_file == without_file


# ---------------------------------------------------------------------------
# Pages root + discovery (L1)
# ---------------------------------------------------------------------------

def test_pages_root_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_PAGES_DIR", str(tmp_path))
    assert discovery.pages_root() == tmp_path.resolve()


def test_pages_root_with_file_set(monkeypatch):
    """Normal install: anchored to the gateway's own ``awm/`` parent."""
    monkeypatch.delenv("AWM_PAGES_DIR", raising=False)
    root = discovery.pages_root()
    assert root.name == "pages"
    assert root.parent.name == "awm"


def test_pages_root_file_and_namespace_agree(monkeypatch):
    """Namespace-package resolution (``__file__`` is ``None``) must not raise
    and must land on the same pages dir as the normal resolution."""
    monkeypatch.delenv("AWM_PAGES_DIR", raising=False)
    with_file = discovery.pages_root()
    monkeypatch.setattr(awm.gateway, "__file__", None, raising=False)
    without_file = discovery.pages_root()  # must not raise Path(None)
    assert with_file == without_file


def _make_page(root, name, *, built=True, prefix=None):
    pkg = root / name
    (pkg / "src").mkdir(parents=True)
    (pkg / "index.html").write_text("<html></html>", encoding="utf-8")
    if prefix is not None:
        (pkg / "prefix.txt").write_text(prefix, encoding="utf-8")
    if built:
        dist = pkg / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html>built</html>", encoding="utf-8")
    return pkg


def test_discover_pages_skips_source_only(tmp_path, monkeypatch):
    """A page with no built ``dist/`` is *buildable-but-not-servable* — skipped."""
    monkeypatch.setenv("AWM_PAGES_DIR", str(tmp_path))
    _make_page(tmp_path, "built", built=True)
    _make_page(tmp_path, "sourceonly", built=False)
    names = {s.name for s in discovery.discover_pages()}
    assert names == {"built"}


def test_discover_pages_prefix_default_and_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_PAGES_DIR", str(tmp_path))
    _make_page(tmp_path, "plain", built=True)
    _make_page(tmp_path, "custom", built=True, prefix="/ui/somewhere")
    by_name = {s.name: s for s in discovery.discover_pages()}
    assert by_name["plain"].prefix == "/ui/plain"
    assert by_name["custom"].prefix == "/ui/somewhere"
    # dist_dir points at the servable static root.
    assert by_name["plain"].dist_dir == str(tmp_path / "plain" / "dist")


def test_discover_page_single(tmp_path, monkeypatch):
    monkeypatch.setenv("AWM_PAGES_DIR", str(tmp_path))
    _make_page(tmp_path, "one", built=True)
    assert discovery.discover_page("one").name == "one"
    assert discovery.discover_page("missing") is None
    _make_page(tmp_path, "unbuilt", built=False)
    assert discovery.discover_page("unbuilt") is None  # no dist ⇒ not servable

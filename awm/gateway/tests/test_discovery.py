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

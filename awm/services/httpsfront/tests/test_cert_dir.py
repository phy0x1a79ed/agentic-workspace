"""Where httpsfront keeps its leaf."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def test_the_leaf_can_live_outside_the_checkout(monkeypatch, tmp_path):
    import importlib

    from awm.httpsfront import hub_adapter

    monkeypatch.delenv("AWM_EDGE_CERT_DIR", raising=False)
    assert importlib.reload(hub_adapter).CERT_DIR.name == ".certs"
    monkeypatch.setenv("AWM_EDGE_CERT_DIR", str(tmp_path))
    assert importlib.reload(hub_adapter).CERT_DIR == tmp_path
    monkeypatch.delenv("AWM_EDGE_CERT_DIR")
    importlib.reload(hub_adapter)

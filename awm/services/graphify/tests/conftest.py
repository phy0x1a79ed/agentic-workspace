"""Shared fixtures for the graphify service tests.

Cross-dist imports stay lazy (inside fixtures/tests) per the per-dist test
runner — only this dist's source root + the shared components are on PYTHONPATH.
"""

import json

import pytest


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the service's output root at a tmp dir.

    ``runner.out_dir_for`` reads the module-global ``SERVICES_DIR``; redirect it
    so tests never touch the real ``$AWM_DIR/services/graphify/``.
    """
    from awm.graphify import runner

    root = tmp_path / "services"
    monkeypatch.setattr(runner, "SERVICES_DIR", root)
    return root


@pytest.fixture
def fake_target(tmp_path):
    """A throwaway directory standing in for a source tree to index."""
    tgt = tmp_path / "tree"
    tgt.mkdir()
    return tgt


def write_graph(out_dir, *, nodes, edges):
    """Write a minimal graphify-out/graph.json under ``out_dir``."""
    gj = out_dir / "graphify-out" / "graph.json"
    gj.parent.mkdir(parents=True, exist_ok=True)
    gj.write_text(json.dumps({
        "nodes": [{"id": f"n{i}"} for i in range(nodes)],
        "edges": [{"source": "n0", "target": "n1"} for _ in range(edges)],
        "hyperedges": [],
        "input_tokens": 0,
        "output_tokens": 0,
    }))
    return gj

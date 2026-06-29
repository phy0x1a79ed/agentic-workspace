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
    """Write a minimal graphify-out/graph.json + empty manifest.json under ``out_dir``.

    The empty manifest ensures ``is_stale`` returns False (no recorded files to
    check), so tests using pre-built graphs don't trigger auto-rebuild.
    """
    gj = out_dir / "graphify-out" / "graph.json"
    gj.parent.mkdir(parents=True, exist_ok=True)
    gj.write_text(json.dumps({
        "nodes": [{"id": f"n{i}", "label": f"node{i}", "file_type": "code",
                   "source_file": f"/fake/file{i}.py", "source_location": f"L{i+1}",
                   "_origin": "ast"}
                  for i in range(nodes)],
        "edges": [{"source": "n0", "target": "n1", "relation": "calls",
                   "context": "call", "confidence": "EXTRACTED", "weight": 1.0}
                  for _ in range(edges)],
        "hyperedges": [],
        "input_tokens": 0,
        "output_tokens": 0,
    }))
    # Empty manifest → is_stale returns (False, 0), no auto-rebuild in read tests.
    mj = out_dir / "graphify-out" / "manifest.json"
    mj.write_text("{}")
    return gj


def write_rich_graph(out_dir):
    """Write a small but realistic graph.json for find/refs/staleness tests.

    Nodes:
      n0  gateway.py (file)
      n1  serve() in gateway.py
      n2  create_session() in sessions.py
      n3  register() in gateway.py (second occurrence — ambiguous with n4)
      n4  register() in mcp.py

    Edges:
      n1 --calls--> n2
      n1 --calls--> n3
      n0 --contains--> n1
      n0 --contains--> n3
    """
    nodes = [
        {"id": "n0", "label": "gateway.py", "file_type": "code",
         "source_file": "/awm/gateway/gateway.py", "source_location": "L1", "_origin": "ast"},
        {"id": "n1", "label": "serve()", "file_type": "code",
         "source_file": "/awm/gateway/gateway.py", "source_location": "L10", "_origin": "ast"},
        {"id": "n2", "label": "create_session()", "file_type": "code",
         "source_file": "/awm/sessions.py", "source_location": "L5", "_origin": "ast"},
        {"id": "n3", "label": "register()", "file_type": "code",
         "source_file": "/awm/gateway/gateway.py", "source_location": "L20", "_origin": "ast"},
        {"id": "n4", "label": "register()", "file_type": "code",
         "source_file": "/awm/mcp.py", "source_location": "L3", "_origin": "ast"},
    ]
    edges = [
        {"source": "n1", "target": "n2", "relation": "calls", "context": "call",
         "confidence": "EXTRACTED", "source_file": "/awm/gateway/gateway.py",
         "source_location": "L11", "weight": 1.0},
        {"source": "n1", "target": "n3", "relation": "calls", "context": "call",
         "confidence": "EXTRACTED", "source_file": "/awm/gateway/gateway.py",
         "source_location": "L12", "weight": 1.0},
        {"source": "n0", "target": "n1", "relation": "contains", "context": None,
         "confidence": "EXTRACTED", "source_file": "/awm/gateway/gateway.py",
         "source_location": "L10", "weight": 1.0},
        {"source": "n0", "target": "n3", "relation": "contains", "context": None,
         "confidence": "EXTRACTED", "source_file": "/awm/gateway/gateway.py",
         "source_location": "L20", "weight": 1.0},
    ]
    gj = out_dir / "graphify-out" / "graph.json"
    gj.parent.mkdir(parents=True, exist_ok=True)
    gj.write_text(json.dumps({
        "nodes": nodes, "edges": edges, "hyperedges": [],
        "input_tokens": 0, "output_tokens": 0,
    }))
    mj = out_dir / "graphify-out" / "manifest.json"
    mj.write_text("{}")
    return gj


def write_manifest(out_dir, entries: dict):
    """Write a manifest.json with specific entries for staleness tests."""
    mj = out_dir / "graphify-out" / "manifest.json"
    mj.parent.mkdir(parents=True, exist_ok=True)
    mj.write_text(json.dumps(entries))
    return mj

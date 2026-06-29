"""Unit tests for the graphify service runner — the graphify binary is mocked.

Covers target resolution, per-target output keying, the build subprocess
contract (argv + LLM-key stripping), text-returning reads, and status. No real
graphify binary or network is exercised.
"""

import subprocess
import types

import pytest

from awm.graphify import runner
from conftest import write_graph


# -- binary resolution ------------------------------------------------------


def test_graphify_bin_prefers_env(monkeypatch):
    monkeypatch.setenv("GRAPHIFY_BIN", "/opt/graphify/bin/graphify")
    assert runner.graphify_bin() == "/opt/graphify/bin/graphify"


def test_graphify_bin_missing_raises(monkeypatch):
    monkeypatch.delenv("GRAPHIFY_BIN", raising=False)
    monkeypatch.setattr(runner.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="graphify binary not found"):
        runner.graphify_bin()


# -- target resolution ------------------------------------------------------


def test_resolve_target_explicit(fake_target):
    assert runner.resolve_target(str(fake_target)) == fake_target.resolve()


def test_resolve_target_env(monkeypatch, fake_target):
    monkeypatch.setenv("GRAPHIFY_TARGET", str(fake_target))
    assert runner.resolve_target() == fake_target.resolve()


def test_resolve_target_default_finds_awm_root(monkeypatch, tmp_path):
    # Build a fake .../awm/services/graphify/awm/graphify/runner.py layout.
    awm_root = tmp_path / "awm"
    (awm_root / "services").mkdir(parents=True)
    (awm_root / "gateway").mkdir()
    start = awm_root / "services" / "graphify" / "awm" / "graphify"
    start.mkdir(parents=True)
    monkeypatch.delenv("GRAPHIFY_TARGET", raising=False)
    assert runner._find_awm_root(start) == awm_root


def test_resolve_target_rejects_nonexistent():
    with pytest.raises(RuntimeError, match="not a directory"):
        runner.resolve_target("/no/such/tree/anywhere")


# -- output keying ----------------------------------------------------------


def test_out_dir_is_per_target_and_under_data_dir(data_dir, tmp_path):
    a = runner.out_dir_for(tmp_path / "treeA")
    b = runner.out_dir_for(tmp_path / "treeB")
    assert a != b
    assert data_dir in a.parents and data_dir in b.parents
    assert a.parent == data_dir / "graphify"  # SERVICES_DIR/graphify/<hash>


# -- build ------------------------------------------------------------------


def test_build_argv_and_strips_llm_keys(monkeypatch, data_dir, fake_target):
    monkeypatch.setenv("GRAPHIFY_BIN", "/bin/graphify")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-stripped")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-also-stripped")

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        # graphify writes graphify-out/graph.json under the --out dir.
        out = cmd[cmd.index("--out") + 1]
        write_graph(runner.Path(out), nodes=6, edges=8)
        return types.SimpleNamespace(returncode=0, stdout="wrote … 6 nodes, 8 edges", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = runner.build(str(fake_target))

    assert captured["cmd"][:2] == ["/bin/graphify", "extract"]
    assert "--no-cluster" in captured["cmd"] and "--out" in captured["cmd"]
    # LLM keys never reach the build subprocess.
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert "OPENAI_API_KEY" not in captured["env"]
    assert res["nodes"] == 6 and res["edges"] == 8
    assert res["summary"].endswith("6 nodes, 8 edges")


def test_build_raises_on_failure(monkeypatch, data_dir, fake_target):
    monkeypatch.setenv("GRAPHIFY_BIN", "/bin/graphify")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    with pytest.raises(RuntimeError, match="graphify extract failed.*boom"):
        runner.build(str(fake_target))


# -- query / path -----------------------------------------------------------


def test_query_requires_a_graph(data_dir, fake_target, monkeypatch):
    monkeypatch.setenv("GRAPHIFY_BIN", "/bin/graphify")
    with pytest.raises(RuntimeError, match="run graphify_build first"):
        runner.query("anything", str(fake_target))


def test_query_returns_stdout_text(monkeypatch, data_dir, fake_target):
    monkeypatch.setenv("GRAPHIFY_BIN", "/bin/graphify")
    write_graph(runner.out_dir_for(fake_target.resolve()), nodes=2, edges=1)

    def fake_run(cmd, **kw):
        assert cmd[1] == "query" and "--graph" in cmd
        return types.SimpleNamespace(returncode=0, stdout="Traversal: BFS ...\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = runner.query("how does X reach Y", str(fake_target))
    assert res["result"] == "Traversal: BFS ..."
    assert res["graph"].endswith("graph.json")


def test_path_returns_stdout_text(monkeypatch, data_dir, fake_target):
    monkeypatch.setenv("GRAPHIFY_BIN", "/bin/graphify")
    write_graph(runner.out_dir_for(fake_target.resolve()), nodes=3, edges=2)

    def fake_run(cmd, **kw):
        assert cmd[1] == "path" and cmd[2] == "A" and cmd[3] == "B"
        return types.SimpleNamespace(returncode=0, stdout="Shortest path (1 hops):\n A --> B", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = runner.path("A", "B", str(fake_target))
    assert "Shortest path" in res["result"]


# -- status -----------------------------------------------------------------


def test_status_absent(data_dir, fake_target):
    st = runner.status(str(fake_target))
    assert st["exists"] is False
    assert st["target"] == str(fake_target.resolve())


def test_status_present_counts(data_dir, fake_target):
    write_graph(runner.out_dir_for(fake_target.resolve()), nodes=5, edges=7)
    st = runner.status(str(fake_target))
    assert st["exists"] is True
    assert st["nodes"] == 5 and st["edges"] == 7
    assert "built_at" in st and st["graph"].endswith("graph.json")

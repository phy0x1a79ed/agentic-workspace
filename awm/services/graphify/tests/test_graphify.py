"""Unit tests for the graphify service runner — the graphify binary is mocked.

Covers target resolution, per-target output keying, the build subprocess
contract (argv + LLM-key stripping), staleness detection, output parsing,
all read verbs (query/path/explain/affected), and the in-memory verbs
(find/refs). No real graphify binary or network is exercised.
"""

import json
import subprocess
import types

import pytest

from awm.graphify import runner
from conftest import write_graph, write_manifest, write_rich_graph


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
    assert a.parent == data_dir / "graphify"


# -- staleness --------------------------------------------------------------


def test_is_stale_no_graph(data_dir, fake_target):
    stale, changed = runner.is_stale(str(fake_target))
    assert stale is True and changed == 0


def test_is_stale_empty_manifest(data_dir, fake_target):
    write_graph(runner.out_dir_for(fake_target.resolve()), nodes=2, edges=1)
    stale, changed = runner.is_stale(str(fake_target))
    assert stale is False and changed == 0


def test_is_stale_detects_changed_file(data_dir, fake_target, tmp_path):
    # Write a source file and record its mtime in the manifest.
    src = fake_target / "foo.py"
    src.write_text("x = 1")
    old_mtime = src.stat().st_mtime

    out = runner.out_dir_for(fake_target.resolve())
    write_graph(out, nodes=1, edges=0)
    write_manifest(out, {"foo.py": {"mtime": old_mtime - 100, "ast_hash": "aaa", "semantic_hash": "aaa"}})

    stale, changed = runner.is_stale(str(fake_target))
    assert stale is True and changed == 1


def test_is_stale_up_to_date(data_dir, fake_target):
    src = fake_target / "foo.py"
    src.write_text("x = 1")
    current_mtime = src.stat().st_mtime

    out = runner.out_dir_for(fake_target.resolve())
    write_graph(out, nodes=1, edges=0)
    write_manifest(out, {"foo.py": {"mtime": current_mtime, "ast_hash": "aaa", "semantic_hash": "aaa"}})

    stale, changed = runner.is_stale(str(fake_target))
    assert stale is False and changed == 0


# -- build ------------------------------------------------------------------


def test_build_argv_and_strips_llm_keys(monkeypatch, data_dir, fake_target):
    monkeypatch.setenv("GRAPHIFY_BIN", "/bin/graphify")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-stripped")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-also-stripped")

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        out = cmd[cmd.index("--out") + 1]
        write_graph(runner.Path(out), nodes=6, edges=8)
        return types.SimpleNamespace(returncode=0, stdout="wrote … 6 nodes, 8 edges", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = runner.build(str(fake_target))

    assert captured["cmd"][:2] == ["/bin/graphify", "extract"]
    assert "--no-cluster" in captured["cmd"] and "--out" in captured["cmd"]
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


def test_build_wipes_prior_output_for_full_rebuild(monkeypatch, data_dir, fake_target):
    """``graphify extract`` is incremental+destructive (a one-file edit collapses
    the graph to only that file's nodes). ``_do_build`` must wipe the prior
    ``graphify-out/`` so every build is a full one — else auto-rebuild-on-stale
    would gut the graph instead of refreshing it. Assert the prior output is gone
    by the time extract runs."""
    monkeypatch.setenv("GRAPHIFY_BIN", "/bin/graphify")
    out = runner.out_dir_for(fake_target.resolve())
    # Seed a pre-existing (stale) build with a sentinel that a full wipe removes.
    write_graph(out, nodes=4946, edges=9731)
    sentinel = out / "graphify-out" / "STALE_NODE_DATA"
    sentinel.write_text("nodes from files that no longer changed")

    saw_sentinel = {}

    def fake_run(cmd, **kw):
        # By the time extract runs, the prior graphify-out must be gone.
        saw_sentinel["present"] = sentinel.exists()
        write_graph(runner.Path(cmd[cmd.index("--out") + 1]), nodes=3, edges=2)
        return types.SimpleNamespace(returncode=0, stdout="wrote … 3 nodes, 2 edges", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner.build(str(fake_target))
    assert saw_sentinel["present"] is False  # prior output wiped before extract


# -- query ------------------------------------------------------------------


def test_query_auto_builds_when_missing(monkeypatch, data_dir, fake_target):
    """When no graph exists, query should auto-build before running."""
    monkeypatch.setenv("GRAPHIFY_BIN", "/bin/graphify")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd[1])  # record sub-command
        if cmd[1] == "extract":
            write_graph(runner.out_dir_for(fake_target.resolve()), nodes=2, edges=1)
            return types.SimpleNamespace(returncode=0, stdout="wrote …", stderr="")
        # query call
        return types.SimpleNamespace(returncode=0, stdout="Traversal: BFS\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = runner.query("anything", target=str(fake_target))
    assert "extract" in calls
    assert "query" in calls
    assert "text" in res


def test_query_returns_structured_output(monkeypatch, data_dir, fake_target):
    monkeypatch.setenv("GRAPHIFY_BIN", "/bin/graphify")
    write_graph(runner.out_dir_for(fake_target.resolve()), nodes=2, edges=1)

    cli_output = (
        "Traversal: BFS depth=2 | Start: [serve()] | 2 nodes found\n"
        "\n"
        "NODE serve() [src=/awm/gateway.py loc=L10 community=Gateway]\n"
        "NODE create_session() [src=/awm/sessions.py loc=L5 community=Sessions]\n"
        "EDGE serve() --calls [EXTRACTED context=call]--> create_session()\n"
    )

    def fake_run(cmd, **kw):
        assert cmd[1] == "query" and "--graph" in cmd
        return types.SimpleNamespace(returncode=0, stdout=cli_output, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = runner.query("how does serve reach create_session", target=str(fake_target))
    assert res["nodes"] == [
        {"label": "serve()", "file": "/awm/gateway.py", "line": "L10"},
        {"label": "create_session()", "file": "/awm/sessions.py", "line": "L5"},
    ]
    assert res["edges"][0]["source"] == "serve()"
    assert res["edges"][0]["relation"] == "calls"
    assert res["edges"][0]["target"] == "create_session()"
    assert res["graph"].endswith("graph.json")
    assert "truncated" not in res


def test_query_passes_context_and_budget(monkeypatch, data_dir, fake_target):
    monkeypatch.setenv("GRAPHIFY_BIN", "/bin/graphify")
    write_graph(runner.out_dir_for(fake_target.resolve()), nodes=1, edges=0)
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="NODE x [src=a.py loc=L1 community=X]\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner.query("X?", context=["call", "import"], budget=5000, target=str(fake_target))

    assert "--context" in captured["cmd"]
    ctx_indices = [i for i, v in enumerate(captured["cmd"]) if v == "--context"]
    assert len(ctx_indices) == 2
    assert captured["cmd"][ctx_indices[0] + 1] == "call"
    assert captured["cmd"][ctx_indices[1] + 1] == "import"
    assert "--budget" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--budget") + 1] == "5000"


def test_query_reports_truncation(monkeypatch, data_dir, fake_target):
    monkeypatch.setenv("GRAPHIFY_BIN", "/bin/graphify")
    write_graph(runner.out_dir_for(fake_target.resolve()), nodes=1, edges=0)
    truncated_output = (
        "NODE foo() [src=a.py loc=L1 community=X]\n"
        "... (truncated — 42 more nodes cut by ~2000-token budget. Narrow with context_filter=['call'])\n"
    )
    monkeypatch.setattr(subprocess, "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=truncated_output, stderr=""))
    res = runner.query("anything", target=str(fake_target))
    assert res["truncated"] is True
    assert res["extra"] == 42


# -- path -------------------------------------------------------------------


def test_path_returns_structured_output(monkeypatch, data_dir, fake_target):
    monkeypatch.setenv("GRAPHIFY_BIN", "/bin/graphify")
    write_graph(runner.out_dir_for(fake_target.resolve()), nodes=3, edges=2)

    def fake_run(cmd, **kw):
        assert cmd[1] == "path" and cmd[2] == "A" and cmd[3] == "B"
        return types.SimpleNamespace(
            returncode=0, stdout="Shortest path (1 hops):\n  A --> B", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = runner.path("A", "B", str(fake_target))
    assert res["steps"] == ["A", "B"]
    assert "Shortest path" in res["text"]


# -- explain ----------------------------------------------------------------


def test_explain_parses_structured_output(monkeypatch, data_dir, fake_target):
    monkeypatch.setenv("GRAPHIFY_BIN", "/bin/graphify")
    write_graph(runner.out_dir_for(fake_target.resolve()), nodes=2, edges=1)

    explain_output = (
        "Node: serve()\n"
        "  ID:        n1\n"
        "  Source:    /awm/gateway.py L10\n"
        "  Type:      code\n"
        "  Community: Gateway\n"
        "  Degree:    3\n"
        "\n"
        "Connections (3):\n"
        "  --> create_session() [calls] [EXTRACTED]\n"
        "  <-- main() [calls] [EXTRACTED]\n"
    )

    def fake_run(cmd, **kw):
        assert cmd[1] == "explain" and cmd[2] == "serve()" and "--graph" in cmd
        return types.SimpleNamespace(returncode=0, stdout=explain_output, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = runner.explain("serve()", str(fake_target))
    assert res["label"] == "serve()"
    assert res["id"] == "n1"
    assert res["file"] == "/awm/gateway.py"
    assert res["line"] == "L10"
    assert res["type"] == "code"
    assert res["community"] == "Gateway"
    assert res["degree"] == 3
    assert len(res["connections"]) == 2
    assert res["graph"].endswith("graph.json")


# -- affected ---------------------------------------------------------------


def test_affected_argv(monkeypatch, data_dir, fake_target):
    monkeypatch.setenv("GRAPHIFY_BIN", "/bin/graphify")
    write_graph(runner.out_dir_for(fake_target.resolve()), nodes=2, edges=1)
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return types.SimpleNamespace(returncode=0,
            stdout="NODE caller() [src=a.py loc=L1 community=X]\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = runner.affected("serve()", depth=3, relations=["calls", "imports"],
                          target=str(fake_target))
    assert captured["cmd"][1] == "affected"
    assert captured["cmd"][2] == "serve()"
    assert "--depth" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--depth") + 1] == "3"
    rel_indices = [i for i, v in enumerate(captured["cmd"]) if v == "--relation"]
    assert len(rel_indices) == 2
    assert res["nodes"][0]["label"] == "caller()"


# -- find -------------------------------------------------------------------


def test_find_exact_match_first(data_dir, fake_target):
    write_rich_graph(runner.out_dir_for(fake_target.resolve()))
    res = runner.find("serve()", target=str(fake_target))
    assert res["count"] >= 1
    assert res["matches"][0]["label"] == "serve()"  # exact match ranked first


def test_find_substring_match(data_dir, fake_target):
    write_rich_graph(runner.out_dir_for(fake_target.resolve()))
    res = runner.find("session", target=str(fake_target))
    labels = [m["label"] for m in res["matches"]]
    assert "create_session()" in labels


def test_find_ambiguous_returns_all(data_dir, fake_target):
    write_rich_graph(runner.out_dir_for(fake_target.resolve()))
    res = runner.find("register", target=str(fake_target))
    # Rich graph has two register() nodes (n3 in gateway.py, n4 in mcp.py)
    assert res["count"] >= 2
    files = [m["file"] for m in res["matches"]]
    assert any("gateway" in (f or "") for f in files)
    assert any("mcp" in (f or "") for f in files)


def test_find_returns_file_and_line(data_dir, fake_target):
    write_rich_graph(runner.out_dir_for(fake_target.resolve()))
    res = runner.find("serve", target=str(fake_target))
    m = res["matches"][0]
    assert m["file"] is not None
    assert m["line"] is not None


def test_find_limit(data_dir, fake_target):
    write_rich_graph(runner.out_dir_for(fake_target.resolve()))
    res = runner.find("e", limit=2, target=str(fake_target))  # matches many nodes
    assert len(res["matches"]) <= 2
    if res["count"] > 2:
        assert res["truncated"] is True


def test_find_no_match(data_dir, fake_target):
    write_rich_graph(runner.out_dir_for(fake_target.resolve()))
    res = runner.find("xyzzy_nonexistent", target=str(fake_target))
    assert res["count"] == 0
    assert res["matches"] == []


# -- refs -------------------------------------------------------------------


def test_refs_out_returns_callees(data_dir, fake_target):
    write_rich_graph(runner.out_dir_for(fake_target.resolve()))
    # serve() --calls--> create_session() and register()
    res = runner.refs("serve()", direction="out", target=str(fake_target))
    assert len(res["matched_nodes"]) == 1
    out_labels = [e["other_label"] for e in res["out"]]
    assert "create_session()" in out_labels or "register()" in out_labels


def test_refs_in_returns_callers(data_dir, fake_target):
    write_rich_graph(runner.out_dir_for(fake_target.resolve()))
    # create_session() is called by serve()
    res = runner.refs("create_session", direction="in", target=str(fake_target))
    in_labels = [e["other_label"] for e in res["in"]]
    assert "serve()" in in_labels


def test_refs_both_direction(data_dir, fake_target):
    write_rich_graph(runner.out_dir_for(fake_target.resolve()))
    res = runner.refs("serve()", direction="both", target=str(fake_target))
    assert "in" in res and "out" in res


def test_refs_invalid_direction(data_dir, fake_target):
    write_rich_graph(runner.out_dir_for(fake_target.resolve()))
    with pytest.raises(ValueError, match="direction must be"):
        runner.refs("anything", direction="sideways", target=str(fake_target))


def test_refs_ambiguous_returns_all_matched(data_dir, fake_target):
    write_rich_graph(runner.out_dir_for(fake_target.resolve()))
    # "register" matches n3 (gateway.py) and n4 (mcp.py)
    res = runner.refs("register", direction="in", target=str(fake_target))
    assert len(res["matched_nodes"]) >= 2


def test_refs_no_match(data_dir, fake_target):
    write_rich_graph(runner.out_dir_for(fake_target.resolve()))
    res = runner.refs("xyzzy_nonexistent", target=str(fake_target))
    assert res["matched_nodes"] == []
    assert res["in"] == [] and res["out"] == []


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


def test_status_includes_staleness(data_dir, fake_target):
    write_graph(runner.out_dir_for(fake_target.resolve()), nodes=2, edges=1)
    st = runner.status(str(fake_target))
    assert "stale" in st
    assert "changed" in st
    assert st["stale"] is False  # empty manifest → fresh


def test_status_never_rebuilds(monkeypatch, data_dir, fake_target):
    """status() must not call subprocess even when stale."""
    monkeypatch.setenv("GRAPHIFY_BIN", "/bin/graphify")
    out = runner.out_dir_for(fake_target.resolve())
    write_graph(out, nodes=1, edges=0)
    # Mark as stale
    src = fake_target / "foo.py"
    src.write_text("x = 1")
    write_manifest(out, {"foo.py": {"mtime": 0, "ast_hash": "old", "semantic_hash": "old"}})

    ran_subprocess = []
    monkeypatch.setattr(subprocess, "run",
        lambda *a, **k: ran_subprocess.append(True) or types.SimpleNamespace(returncode=0, stdout="", stderr=""))

    st = runner.status(str(fake_target))
    assert st["stale"] is True
    assert not ran_subprocess, "status() must never run a subprocess"

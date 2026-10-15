"""The MCP proxies stamp a local search query with the calling session's scope, read from its working directory."""

from awm.gateway import mcp_http


def _worktree(root, *parts):
    path = root.joinpath(*parts)
    path.mkdir(parents=True)
    (path / ".git").write_text("gitdir: ../.bare/worktrees/x\n")
    return path


def test_a_scope_worktree_names_its_project_and_scope(tmp_path):
    flat = _worktree(tmp_path, "projects", "awm", "svc-kb")
    nested = _worktree(tmp_path, "projects", "metasmith", "engcyc", "dev")
    (flat / "src" / "deep").mkdir(parents=True)
    assert mcp_http.scope_of(flat / "src" / "deep") == "awm/svc-kb"
    assert mcp_http.scope_of(nested) == "metasmith/engcyc/dev"


def test_outside_a_scope_there_is_no_context(tmp_path):
    root = _worktree(tmp_path, "workspace")
    assert mcp_http.scope_of(root) == ""
    assert mcp_http.scope_of(tmp_path) == ""


def test_only_a_local_search_query_without_a_context_is_stamped(tmp_path):
    cwd = str(_worktree(tmp_path, "projects", "awm", "svc-kb"))
    call = {"verb": "query", "args": {"query": "pagerank"}}
    assert mcp_http.with_search_context("search", call, cwd)["args"]["context"] == "awm/svc-kb"
    assert call["args"] == {"query": "pagerank"}
    for name, arguments in (("search", {"verb": "get", "args": {"id": "x"}}),
                            ("journal", {"verb": "query", "args": {"query": "q"}}),
                            ("search", {"verb": "query", "args": {"query": "q"}, "peer": "capella"}),
                            ("search", {"verb": "query", "args": {"query": "q", "context": "bio"}})):
        assert mcp_http.with_search_context(name, arguments, cwd) == arguments

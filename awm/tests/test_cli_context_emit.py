"""Tests for the `awm context emit` CLI subcommand.

This command feeds the Claude Code SessionStart hook (and equivalent
opencode wiring): on a fresh session in a scope worktree, the hook calls
`awm context emit --cwd "$CLAUDE_PROJECT_DIR"` and the printed bytes are
spliced into the agent's initial context. The contract:

- Walks up from cwd to find the workspace root (marked by `WORKSPACE.md`).
- Emits in general → specific order: `<workspace-context>`, optional
  `<agents-context>` (cwd-local only, never the workspace-root copy),
  optional `<scope-context>`.
- Silent (no stdout, exit 0) when cwd is outside any workspace.
- Each missing layer (workspace, agents, scope) silently skipped.
- Never includes `.awm/history.md` or `.awm/artifacts.md` (too large;
  load-on-demand).
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from awm.cli import app


runner = CliRunner()


# ---------------------------------------------------------------------------
# Original contract — scope-context emission
# ---------------------------------------------------------------------------


def test_emits_wrapped_when_present(tmp_path: Path) -> None:
    # Mark tmp_path as a workspace; cwd is a scope subdir under it.
    (tmp_path / "WORKSPACE.md").write_text("# workspace\n")
    scope = tmp_path / "scope"
    scope.mkdir()
    awm = scope / ".awm"
    awm.mkdir()
    (awm / "context.md").write_text("# scope ritual\n\nDo the things.\n")

    result = runner.invoke(app, ["context", "emit", "--cwd", str(scope)])

    assert result.exit_code == 0
    assert '<scope-context path=".awm/context.md">' in result.stdout
    assert "# scope ritual" in result.stdout
    assert "Do the things." in result.stdout
    assert "</scope-context>" in result.stdout


def test_silent_when_no_context_md(tmp_path: Path) -> None:
    # Workspace exists, but the scope has no .awm/context.md — workspace
    # block still emits, scope block does not.
    (tmp_path / "WORKSPACE.md").write_text("# workspace\n")
    scope = tmp_path / "scope"
    scope.mkdir()

    result = runner.invoke(app, ["context", "emit", "--cwd", str(scope)])
    assert result.exit_code == 0
    assert "<workspace-context" in result.stdout
    assert "<scope-context" not in result.stdout


def test_silent_when_awm_exists_but_no_context_md(tmp_path: Path) -> None:
    # .awm/ exists with history/artifacts but no context.md — scope block
    # silently skipped; workspace block still emits.
    (tmp_path / "WORKSPACE.md").write_text("# workspace\n")
    scope = tmp_path / "scope"
    scope.mkdir()
    awm = scope / ".awm"
    awm.mkdir()
    (awm / "history.md").write_text("# big history\n" * 1000)
    (awm / "artifacts.md").write_text("# big artifacts\n" * 1000)

    result = runner.invoke(app, ["context", "emit", "--cwd", str(scope)])

    assert result.exit_code == 0
    assert "<workspace-context" in result.stdout
    assert "<scope-context" not in result.stdout
    assert "big history" not in result.stdout
    assert "big artifacts" not in result.stdout


def test_does_not_include_history_or_artifacts(tmp_path: Path) -> None:
    """History and artifacts are reference material, not orientation —
    auto-load would balloon every session by hundreds of KB."""
    (tmp_path / "WORKSPACE.md").write_text("# workspace\n")
    scope = tmp_path / "scope"
    scope.mkdir()
    awm = scope / ".awm"
    awm.mkdir()
    (awm / "context.md").write_text("ctx body\n")
    (awm / "history.md").write_text("HISTORY_SENTINEL\n")
    (awm / "artifacts.md").write_text("ARTIFACTS_SENTINEL\n")

    result = runner.invoke(app, ["context", "emit", "--cwd", str(scope)])

    assert result.exit_code == 0
    assert "HISTORY_SENTINEL" not in result.stdout
    assert "ARTIFACTS_SENTINEL" not in result.stdout
    assert "ctx body" in result.stdout


# ---------------------------------------------------------------------------
# Walk-up + 3-tier ordering
# ---------------------------------------------------------------------------


def test_emit_outside_workspace_exits_silently(tmp_path: Path) -> None:
    # tmp_path has no WORKSPACE.md upstream — exit 0 with empty output.
    result = runner.invoke(app, ["context", "emit", "--cwd", str(tmp_path)])
    assert result.exit_code == 0
    assert result.stdout == ""


def test_emit_general_to_specific_order(tmp_path: Path) -> None:
    # workspace at tmp_path; cwd is a scope subdir with its own AGENTS.md
    # + .awm/context.md. Verify the three blocks appear in workspace →
    # agents → scope order.
    (tmp_path / "WORKSPACE.md").write_text("WS_SENTINEL\n")
    scope = tmp_path / "scope"
    scope.mkdir()
    (scope / "AGENTS.md").write_text("AGENTS_SENTINEL\n")
    awm = scope / ".awm"
    awm.mkdir()
    (awm / "context.md").write_text("SCOPE_SENTINEL\n")

    result = runner.invoke(app, ["context", "emit", "--cwd", str(scope)])

    assert result.exit_code == 0
    out = result.stdout
    i_ws = out.find("<workspace-context")
    i_ag = out.find("<agents-context")
    i_sc = out.find("<scope-context")
    assert i_ws != -1 and i_ag != -1 and i_sc != -1, out
    assert i_ws < i_ag < i_sc, f"expected workspace→agents→scope, got {out!r}"
    assert "WS_SENTINEL" in out
    assert "AGENTS_SENTINEL" in out
    assert "SCOPE_SENTINEL" in out


def test_emit_skips_workspace_level_agents_md(tmp_path: Path) -> None:
    # cwd == workspace root with AGENTS.md present — workspace block
    # emits, agents block does NOT (cwd-only rule + cwd≠workspace_root).
    (tmp_path / "WORKSPACE.md").write_text("WS_SENTINEL\n")
    (tmp_path / "AGENTS.md").write_text("AGENTS_SENTINEL\n")

    result = runner.invoke(app, ["context", "emit", "--cwd", str(tmp_path)])

    assert result.exit_code == 0
    assert "WS_SENTINEL" in result.stdout
    assert "<workspace-context" in result.stdout
    assert "<agents-context" not in result.stdout
    assert "AGENTS_SENTINEL" not in result.stdout


def test_emit_walk_up_finds_workspace_from_nested_cwd(tmp_path: Path) -> None:
    # workspace at tmp_path; cwd is nested 3 levels deep.
    (tmp_path / "WORKSPACE.md").write_text("WS_SENTINEL\n")
    nested = tmp_path / "projects" / "foo" / "bar"
    nested.mkdir(parents=True)
    (nested / "AGENTS.md").write_text("NESTED_AGENTS_SENTINEL\n")

    result = runner.invoke(app, ["context", "emit", "--cwd", str(nested)])

    assert result.exit_code == 0
    assert "WS_SENTINEL" in result.stdout
    assert "NESTED_AGENTS_SENTINEL" in result.stdout
    # The workspace path emitted should be relative-or-absolute back to
    # WORKSPACE.md, not just the basename — check the file existed under
    # the walk-up.
    assert "WORKSPACE.md" in result.stdout
    assert "AGENTS.md" in result.stdout


def test_emit_cwd_agents_md_emitted_for_scope(tmp_path: Path) -> None:
    # Scope subdir with its own AGENTS.md + .awm/context.md (no leading
    # workspace block ordering concern other than the existing tests).
    (tmp_path / "WORKSPACE.md").write_text("WS_SENTINEL\n")
    scope = tmp_path / "scope"
    scope.mkdir()
    (scope / "AGENTS.md").write_text("AGENTS_SENTINEL\n")
    awm = scope / ".awm"
    awm.mkdir()
    (awm / "context.md").write_text("SCOPE_SENTINEL\n")

    result = runner.invoke(app, ["context", "emit", "--cwd", str(scope)])

    assert result.exit_code == 0
    assert "AGENTS_SENTINEL" in result.stdout
    assert "SCOPE_SENTINEL" in result.stdout
    assert '<agents-context path="AGENTS.md">' in result.stdout
    assert '<scope-context path=".awm/context.md">' in result.stdout


def test_emit_outermost_workspace_wins_when_nested(tmp_path: Path) -> None:
    # Models the .bare-worktree-sharing topology: awm-dev scope at
    # projects/awm/dev/ has its own WORKSPACE.md (committed on dev branch),
    # AND the workspace at agentic_workspace/ has WORKSPACE.md. Emit must
    # pick the OUTERMOST one so cwd != workspace_root and the agents block
    # emits correctly for the awm-dev scope.
    (tmp_path / "WORKSPACE.md").write_text("OUTER_WS\n")
    inner = tmp_path / "projects" / "awm" / "dev"
    inner.mkdir(parents=True)
    (inner / "WORKSPACE.md").write_text("INNER_WS\n")
    (inner / "AGENTS.md").write_text("AGENTS_SENTINEL\n")

    result = runner.invoke(app, ["context", "emit", "--cwd", str(inner)])

    assert result.exit_code == 0
    # The OUTER workspace content wins.
    assert "OUTER_WS" in result.stdout
    # The agents block emits because cwd != outer workspace_root.
    assert "<agents-context" in result.stdout
    assert "AGENTS_SENTINEL" in result.stdout


def test_emit_no_agents_md_no_block(tmp_path: Path) -> None:
    # Scope has .awm/context.md but no cwd-local AGENTS.md — workspace
    # and scope blocks emit, agents block does not.
    (tmp_path / "WORKSPACE.md").write_text("WS_SENTINEL\n")
    scope = tmp_path / "scope"
    scope.mkdir()
    awm = scope / ".awm"
    awm.mkdir()
    (awm / "context.md").write_text("SCOPE_SENTINEL\n")

    result = runner.invoke(app, ["context", "emit", "--cwd", str(scope)])

    assert result.exit_code == 0
    assert "<workspace-context" in result.stdout
    assert "<scope-context" in result.stdout
    assert "<agents-context" not in result.stdout

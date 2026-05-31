"""Tests for the `awm context emit` CLI subcommand.

This command feeds the Claude Code SessionStart hook (and equivalent
opencode wiring): on a fresh session in a scope worktree, the hook calls
`awm context emit --cwd "$CLAUDE_PROJECT_DIR"` and the printed bytes are
spliced into the agent's initial context. The contract:

- Prints the worktree's `.awm/context.md` wrapped in `<scope-context>` when present.
- Silent (no stdout, exit 0) when no `.awm/context.md` exists.
- Never includes `.awm/history.md` or `.awm/artifacts.md` (too large; load-on-demand).
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from awm.cli import app


runner = CliRunner()


def test_emits_wrapped_when_present(tmp_path: Path) -> None:
    awm = tmp_path / ".awm"
    awm.mkdir()
    (awm / "context.md").write_text("# scope ritual\n\nDo the things.\n")

    result = runner.invoke(app, ["context", "emit", "--cwd", str(tmp_path)])

    assert result.exit_code == 0
    assert result.stdout.startswith('<scope-context path=".awm/context.md">\n')
    assert "# scope ritual" in result.stdout
    assert "Do the things." in result.stdout
    assert result.stdout.rstrip().endswith("</scope-context>")


def test_silent_when_no_context_md(tmp_path: Path) -> None:
    # No .awm/ at all.
    result = runner.invoke(app, ["context", "emit", "--cwd", str(tmp_path)])
    assert result.exit_code == 0
    assert result.stdout == ""


def test_silent_when_awm_exists_but_no_context_md(tmp_path: Path) -> None:
    # .awm/ exists with history/artifacts but no context.md — still silent.
    awm = tmp_path / ".awm"
    awm.mkdir()
    (awm / "history.md").write_text("# big history\n" * 1000)
    (awm / "artifacts.md").write_text("# big artifacts\n" * 1000)

    result = runner.invoke(app, ["context", "emit", "--cwd", str(tmp_path)])

    assert result.exit_code == 0
    assert result.stdout == ""


def test_does_not_include_history_or_artifacts(tmp_path: Path) -> None:
    """History and artifacts are reference material, not orientation —
    auto-load would balloon every session by hundreds of KB."""
    awm = tmp_path / ".awm"
    awm.mkdir()
    (awm / "context.md").write_text("ctx body\n")
    (awm / "history.md").write_text("HISTORY_SENTINEL\n")
    (awm / "artifacts.md").write_text("ARTIFACTS_SENTINEL\n")

    result = runner.invoke(app, ["context", "emit", "--cwd", str(tmp_path)])

    assert result.exit_code == 0
    assert "HISTORY_SENTINEL" not in result.stdout
    assert "ARTIFACTS_SENTINEL" not in result.stdout
    assert "ctx body" in result.stdout

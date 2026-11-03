# brief-inject

## Purpose & Contents

This doc holds the review of how an agent receives its scope brief (`scopes.md` R143), and each decision Tony makes on it. The settled decisions go into `scopes.md` as requirements.

## As built

- Step 2 of the startup ritual in `AGENTS.md` tells the agent to read `.awm/context.md`. Nothing hands the brief to the agent.
- A `SessionStart` hook already runs for every Claude Code session (`~/.claude/settings.json`, the reporting hook). A start hook can add text to the agent's context.
- R40 starts a new session in a lobby with no scope. The session enters its scope later with `reflection cd`.
- R135 puts all message injection in the harness plugin of the session.

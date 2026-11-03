# brief-inject

## Purpose & Contents

This doc holds the review of how an agent receives its scope brief (`scopes.md` R143), and each decision Tony makes on it. The settled decisions go into `scopes.md` as requirements.

## As built

- Step 2 of the startup ritual in `AGENTS.md` tells the agent to read `.awm/context.md`. Nothing hands the brief to the agent.
- A `SessionStart` hook already runs for every Claude Code session (`~/.claude/settings.json`, the reporting hook). A start hook can add text to the agent's context.
- R40 starts a new session in a lobby with no scope. The session enters its scope later with `reflection cd`.
- R135 puts all message injection in the harness plugin of the session.

## Decisions

- 2026-11-03: The harness plugin gives the brief to a session each time it enters a scope: through the start hook for a session started in a scope, and in the followup message of `reflection cd` (`scopes.md` R143). Tony: "Good choices. Reflection cd has a followup message. Natural place to inject".
- 2026-11-03: The startup ritual drops its brief step, and its `.awm/` steps are rewritten, in the same change that ships the injection (`scopes.md` R143). Tony: "Make all changes in 1 go at the end, which i think is your recommendation".

## Follow-ups for the build

- Inject the brief from cch's start hook and from the followup of `reflection cd`.
- In the same change, rewrite steps 1 to 3 of the `AGENTS.md` startup ritual and its `.awm/` tree (R142, R143).

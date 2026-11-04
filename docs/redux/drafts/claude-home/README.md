# claude-home drafts

## Purpose & Contents

This folder holds whole-file drafts of Tony's `~/.claude` configuration that match the redux as built. They go live at T27, in the same deploy as the code they describe. Each file replaces its live copy whole. Nothing here is read by a running session.

## Install map

Copy each file to the path on its right, on every node that runs Claude Code.

| Draft | Live path | What changes |
|---|---|---|
| `CLAUDE.md` | `~/.claude/CLAUDE.md` | The standing frame from goal 50708f67. graphify and precedence go. Precedent posts and the `routine deploy` route. The parallel-work pointer |
| `skills/debrief/SKILL.md` | `~/.claude/skills/debrief/SKILL.md` | `tags`, `reply_to` citations, precedent posts (`sessions.md` R12 item 4), the command center |
| `skills/goal-alignment/SKILL.md` | `~/.claude/skills/goal-alignment/SKILL.md` | A goal is a journal post (`fleet.md` R107 item 10) |
| `skills/retire/SKILL.md` | `~/.claude/skills/retire/SKILL.md` | Ends with `reflection stop` or `reflection wait` (`sessions.md` R8, R51) |
| `skills/parallel-work/SKILL.md` | `~/.claude/skills/parallel-work/SKILL.md` | New (`parallel-work.md` R59). `CLAUDE.md` carries its pointer |
| `skills/opal-seat-holder/SKILL.md` | `~/.claude/skills/opal-seat-holder/SKILL.md` | One unpinned session per task (`sessions.md` R67), channel verbs for machines |
| `skills/scatter-gather/SKILL.md` | `~/.claude/skills/scatter-gather/SKILL.md` | Peripherals come from `PROTOCOLS.md` only |
| `skills/dfs-interview/SKILL.md`, `stack.py` | `~/.claude/skills/dfs-interview/` | Decisions go to precedent posts. `quote.py` is unchanged |

## Hooks

`awm/scripts/deploy-hooks.sh` is the one installer of awm's hooks in `~/.claude/settings.json`. It holds no draft here. On each node after the T27 deploy:

1. Run `awm/scripts/deploy-hooks.sh --check`. Expect exit 0.
2. Run `awm/scripts/deploy-hooks.sh`.

It swaps `awm-reporting-hook.py` for `awm-operations-hook.py` and installs the subagent cap on the seat nodes.

## Confirm with Tony at T27

- `CLAUDE.md` § The standing frame holds goal 50708f67 verbatim, from its journal post on altair (`journal fetch {post_id:"50708f67-ea77-4844-a1d8-e486ed0cdccb"}`, peer altair). Tony confirms the text before the copy.

## The chrome-devtools server

The workspace `.mcp.json` is tracked in the awm repo. Change it in the T27 deploy commit, and change `settings.json` in the same step.

- `.mcp.json`: delete the `chrome-devtools` server, which points at a browser on port 19222. It goes when alcor's browser singleton serves the `browser` tool.
- `~/.claude/settings.json`: delete `"chrome-devtools"` from `disabledMcpjsonServers` in the same step.

## Verify after the copy

1. Run `grep -rln "precedence\|graphify\|goal_set\|goal_read\|context.md\|history.md" ~/.claude/CLAUDE.md ~/.claude/skills --exclude-dir=synced`. Expect no hit.
2. Run `grep -c awm-reporting-hook ~/.claude/settings.json`. Expect 0.
3. Start one session. Confirm that it loads the `parallel-work` skill in its skill list.

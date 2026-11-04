# settings.json hook lines

## Purpose & Contents

This file holds the hook changes to `~/.claude/settings.json` that go live at T27, from redux-build/cutover (T18) and redux-build/coop (T17). It names each entry that changes. The rest of `settings.json` stays as it is.

## Make the change

On each node after the T27 deploy:

1. Run `awm/scripts/deploy-hooks.sh --check` to preview.
2. Run `awm/scripts/deploy-hooks.sh`.

Its `operations` row installs `~/.claude/hooks/awm-operations-hook.py`, and its retired list removes every settings entry and the file of `awm-reporting-hook.py`. Its `subagent-cap` row installs `~/.claude/hooks/awm-subagent-cap.py` on mizar and altair. No hand edit is needed.

## What changes

The hook script `awm-reporting-hook.py` becomes `awm-operations-hook.py` on the same three entries. Matchers and timeouts stay.

| Event | Matcher | Timeout | Command |
|---|---|---|---|
| `UserPromptSubmit` | — | 15 | `python3 -S $HOME/.claude/hooks/awm-operations-hook.py 2>/dev/null \|\| true` |
| `PostToolUse` | `Write\|Edit\|ExitPlanMode\|TaskCreate\|TaskUpdate\|TodoWrite` | 15 | the same command |
| `SessionStart` | `clear` | 15 | the same command |

The source is `awm/plugins/operations/hooks/operations_hook.py`, and its settings fragment is `awm/plugins/operations/hooks/awm-operations-settings.json`.

## The subagent cap

The cap on a cooperative session's subagents (`channels.md` R125 item 7) is a cch hook, from redux-build/coop. The script copies it to `~/.claude/hooks/awm-subagent-cap.py` on every node that runs seats, and adds three entries beside the entries already there.

| Event | Matcher | Timeout | Command |
|---|---|---|---|
| `PreToolUse` | `Agent\|Task` | 5 | `python3 -S /home/tony/.claude/hooks/awm-subagent-cap.py 2>/dev/null \|\| true` |
| `SubagentStart` | — | 5 | the same command |
| `SubagentStop` | — | 5 | the same command |

The hook keeps its counts under `$XDG_STATE_HOME/awm/cch/subagents/`, apart from `~/.claude/hook-state/subagents`, which the ExitPlanMode guard reads.

## Verify

1. Run `grep -c awm-reporting-hook ~/.claude/settings.json`. Expect 0.
2. Run `grep -c awm-operations-hook ~/.claude/settings.json`. Expect 3.
3. Run `ls ~/.claude/hooks/awm-reporting-hook.py`. Expect no such file.
4. On a seat node, run `grep -c awm-subagent-cap ~/.claude/settings.json`. Expect 3.

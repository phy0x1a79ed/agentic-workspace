# settings.json hook lines

## Purpose & Contents

This file holds the hook changes to `~/.claude/settings.json` that go live at T27, from redux-build/cutover (T18). It names each entry that changes. The rest of `settings.json` stays as it is.

## Make the change

Run `awm/scripts/deploy-hooks.sh` on each node after the T27 deploy. Its `operations` row installs `~/.claude/hooks/awm-operations-hook.py`, and its retired list removes every settings entry and the file of `awm-reporting-hook.py`. No hand edit is needed.

## What changes

The hook script `awm-reporting-hook.py` becomes `awm-operations-hook.py` on the same three entries. Matchers and timeouts stay.

| Event | Matcher | Timeout | Command |
|---|---|---|---|
| `UserPromptSubmit` | — | 15 | `python3 -S $HOME/.claude/hooks/awm-operations-hook.py 2>/dev/null \|\| true` |
| `PostToolUse` | `Write\|Edit\|ExitPlanMode\|TaskCreate\|TaskUpdate\|TodoWrite` | 15 | the same command |
| `SessionStart` | `clear` | 15 | the same command |

The source is `awm/plugins/operations/hooks/operations_hook.py`, and its settings fragment is `awm/plugins/operations/hooks/awm-operations-settings.json`.

## Verify

1. Run `grep -c awm-reporting-hook ~/.claude/settings.json`. Expect 0.
2. Run `grep -c awm-operations-hook ~/.claude/settings.json`. Expect 3.
3. Run `ls ~/.claude/hooks/awm-reporting-hook.py`. Expect no such file.

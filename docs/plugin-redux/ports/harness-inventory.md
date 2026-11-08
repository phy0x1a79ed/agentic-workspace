# Harness group: node state inventory

## Purpose & Contents

This file records, per node, the state the harness ports carry over, taken before the first land on 2026-11-06 at release 49232bf9. It holds modes, owners, sizes and record counts, never contents. `harness-state.md` names the fate of each path. A dash means the path is absent on that node. Owner is `tony` unless named. Sizes for Claude Code's `jobs`, `projects`, `sessions`, `daemon` and `backups` are omitted, and their counts are top-level entries.

## Node facts

| Node | `enabled.json` `cx` | login-sync timer | awm hook entries in `settings.json` | `AWM_CONDA_ROOT` set |
|---|---|---|---|---|
| mizar | True | none | awm-brief-hook, awm-cx-name-hook, awm-reflection-mode, awm-subagent-cap | no |
| alcor | True | none | awm-cx-name-hook, awm-reflection-mode | no |
| capella | True | absent | awm-cx-name-hook, awm-reflection-mode | no |
| altair | True | none | awm-cx-name-hook, awm-reflection-mode | no |
| shogun | no file | none | none | no |
| lyra | no file | absent | none | no |

## Paths

`$SVC` is `<AWM_DIR>/services`, and `$WS` is the workspace checkout.

| Path | mizar | alcor | capella | altair | shogun | lyra |
|---|---|---|---|---|---|---|
| `$SVC/reflection/trust.json` | 664 13460 B 83 rec | 664 1368 B 10 rec | 664 682 B 5 rec | 664 1962 B 14 rec | – | – |
| `$SVC/reflection/waits.json` | 664 2 B 0 rec | 664 2 B 0 rec | 664 2 B 0 rec | 664 2 B 0 rec | – | – |
| `$SVC/reflection/retired.json` | 664 4646 B 86 rec | 664 542 B 10 rec | 664 272 B 5 rec | 664 1352 B 25 rec | – | – |
| `$SVC/reflection/tickets` | dir 775 69 files 0 B | dir 775 1 files 0 B | – | dir 775 5 files 0 B | – | – |
| `$SVC/reflection/events.jsonl` | 664 82438 B 106 rec | 664 380 B 1 rec | – | 664 17489 B 10 rec | – | – |
| `$SVC/reflection/lost.jsonl` | 664 5448 B 11 rec | – | – | – | – | – |
| `$SVC/reflection/pending` | dir 775 9 files 117 B | dir 775 0 files 0 B | dir 755 0 files 0 B | dir 775 2 files 26 B | – | – |
| `$SVC/reflection` | dir 775 83 files 106111 B | dir 775 5 files 2292 B | dir 755 3 files 956 B | dir 775 11 files 20831 B | – | – |
| `$SVC/keep` | – | – | – | dir 775 0 files 0 B | – | – |
| `$SVC/dsh` | dir 775 2 files 479 B | dir 775 2 files 479 B | dir 775 498 files 24069236 B | dir 775 748 files 36837105 B | dir 775 2 files 479 B | dir 775 2 files 483 B |
| `$SVC/dsh/home` | dir 775 1 files 444 B | dir 775 1 files 444 B | dir 700 496 files 24063949 B | dir 775 285 files 31045985 B | dir 775 1 files 444 B | dir 775 1 files 444 B |
| `$SVC/dsh/home/settings.yaml` | 600 444 B | 600 444 B | 600 682 B | 600 486 B | 600 444 B | 600 444 B |
| `$SVC/dsh/home/.credentials.yaml` | – | – | 600 74 B | – | – | – |
| `$SVC/dsh/home/sessions` | – | – | dir 700 53 files 23827764 B | dir 700 66 files 30766987 B | – | – |
| `$SVC/dsh/home/storages` | – | – | dir 700 2 files 183449 B | dir 700 2 files 219133 B | – | – |
| `$SVC/dsh/home/profiles` | – | – | dir 755 438 files 51952 B | dir 775 210 files 23974 B | – | – |
| `$SVC/dsh/home/.agent-presets` | – | – | link (rel, resolves) 755 4 files 36118 B | dir 775 4 files 35340 B | – | – |
| `$SVC/dsh/node-bin` | 664 35 B | 664 35 B | 664 39 B | 664 39 B | 664 35 B | 664 39 B |
| `$SVC/dsh/dsh.log` | – | – | 644 5248 B 164 rec | 664 37472 B 814 rec | – | – |
| `$WS/awm/services/dsh/.certs` | dir 775 3 files 4174 B | dir 775 3 files 4174 B | dir 755 4 files 4172 B | dir 775 3 files 4121 B | dir 775 4 files 4175 B | dir 775 4 files 4172 B |
| `$WS/awm/services/dsh/.certs/key.pem` | 600 1704 B | 600 1704 B | 600 1704 B | 600 1704 B | 600 1704 B | 600 1704 B |
| `$WS/awm/services/dsh/.sans` | – | – | 644 403 B | – | – | – |
| `$WS/awm/services/dsh/config` | dir 775 7 files 42702 B | dir 775 7 files 42702 B | dir 755 7 files 42702 B | dir 775 7 files 42702 B | dir 775 7 files 42702 B | dir 775 7 files 42702 B |
| `~/.claude/cx` | dir 775 18 files 31907 B | dir 775 3 files 215 B | dir 775 16 files 470 B | dir 775 96 files 10631 B | dir 775 2 files 36 B | dir 775 2 files 36 B |
| `~/.claude/cx/starts` | dir 775 15 files 31692 B | dir 775 0 files 0 B | dir 755 0 files 0 B | dir 775 12 files 7498 B | dir 775 0 files 0 B | dir 775 0 files 0 B |
| `~/.claude/cx/hits.log` | – | – | 664 248 B 3 rec | 664 2911 B 38 rec | – | – |
| `~/.claude/cx/locks` | – | – | dir 775 10 files 0 B | dir 775 78 files 0 B | – | – |
| `~/.claude/cx/nap.fifo` | – | – | fifo 664 | fifo 664 | – | – |
| `~/.claude/awm-reflection-mode.log` | – | – | – | 664 128 B 1 rec | – | – |
| `~/.local/state/awm` | dir 775 33 files 1205 B | dir 775 1 files 173 B | – | – | – | – |
| `~/.claude/sessions` | dir 700 14 files | dir 700 0 files | dir 700 97 files | dir 700 6 files | – | dir 700 4 files |
| `~/.claude/daemon` | dir 700 4 files | dir 700 4 files | dir 700 5 files | dir 700 5 files | – | dir 700 3 files |
| `~/.claude/jobs` | dir 775 16 files | dir 775 2 files | dir 755 18 files | dir 775 25 files | – | – |
| `~/.claude/projects` | dir 700 129 files | dir 700 2 files | dir 700 44 files | dir 775 88 files | – | dir 700 5 files |
| `~/.claude.json` | 600 119282 B 67 rec | 600 77910 B 60 rec | 664 314715 B 92 rec | 664 215834 B 86 rec | – | 600 107869 B 69 rec |
| `~/.claude/settings.json` | 664 7947 B 28 rec | 664 7005 B 28 rec | 664 6987 B 28 rec | 664 7005 B 28 rec | – | 644 266 B 7 rec |
| `~/.claude/backups` | dir 775 9 files | dir 775 5 files | dir 755 45 files | dir 775 41 files | – | dir 755 5 files |
| `~/.claude/hooks/awm-brief-hook.py` | 775 5246 B | – | – | – | – | – |
| `~/.claude/hooks/awm-reflection-mode.py` | 775 7179 B | 775 7179 B | 775 7179 B | link (abs, dangling) 777 76 B | – | – |
| `~/.claude/hooks/awm-cx-name-hook.py` | 775 3680 B | 775 3679 B | 775 3679 B | 775 3679 B | – | – |
| `~/.claude/hooks/awm-subagent-cap.py` | 775 5389 B | – | – | – | – | – |
| `$SVC/dsh/home.bak-20260822-pre-0.1.1` | – | – | – | dir 775 461 files 5753609 B | – | – |

Absent on every node: `$SVC/reflection/unsent.jsonl`, `$SVC/keep/hold.json`, `$SVC/cch`, `$SVC/cch/isolated`, `$SVC/cch/isolated/tokens.json`, `~/.config/systemd/user/awm-isolated-login-sync.service`, `~/.config/systemd/user/awm-isolated-login-sync.timer`.

## Findings

- No node has `$SVC/cch/`, so no isolated session has run and no isolated login copy exists yet.
- shogun and lyra hold no reflection state, and shogun holds no Claude Code records.
- **CAUTION** altair's `~/.claude/hooks/awm-reflection-mode.py` is a link to `awm/services/reflection/hooks/plan_mode_hook.py`, which no longer exists. Its `settings.json` entry runs a dangling path today. `cch hook plan-mode` replaces it.
- capella's `.agent-presets` is a relative link to the tracked `awm/services/dsh/config/agent-presets`. The dsh port moves that folder, so dsh retargets the link at start.
- `~/.claude.json` is mode 664 on capella and altair, against 600 elsewhere. It carries account data. cch never widens a mode, and statecheck keeps it inside a 0700 `.pre-rust/`.
- `$WS/awm/services/dsh/.certs/key.pem` is 600 on every node.

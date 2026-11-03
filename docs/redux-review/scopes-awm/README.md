# scopes-awm

## Purpose & Contents

This doc holds the review of the scope delete guard and the `.awm/` folder: how they are built today, where the build and `docs/redux/scopes.md` R44 disagree, and each decision Tony makes on them. The settled decisions go into `scopes.md` as requirements.

## As built

Surveyed read only on mizar, 2026-11-03.

- **The deployed delete** runs only `git status --porcelain` (`scopes.py:329`). It never checks ignored files. A forced delete removes them with the worktree.
- **The ignored-file check exists only on `feat/redux-sessions`** (`_ignored_files`, `scopes.py:363-385`). It lists git's ignored files. It skips `.awm/`, the DVC cache and temp folders, language caches, and every path under a committed `.dvc` pin. What is left blocks the delete unless `force` is set. A forced delete lists the files it removed and backs up nothing. The hygiene sweep uses the same check (`hygiene.py:128`).
- **Its origin is a survey line, not an incident.** The 2026-10-30 survey listed "the delete check misses ignored files" as a gap (`f08aee22`). That became R44 item 3, and the lane built it to the letter.
- **DVC files show to git as ignored**, because `dvc add` writes a `.gitignore` entry. The check skips them. Removing a worktree unlinks names, never cache bytes, so a committed pin loses nothing. An uncommitted pin shows as untracked, and `git status` already catches it. A file edited inside a pinned chunk without a new `dvc add` is lost with no warning. No check covers that drift.
- **On mizar no scope uses DVC data.** awm's `.gitignore` ignores `data/`. Every `.awm/data` link points to the old shared `<workspace>/data/<project>/`.
- **The check would block 10 of mizar's 33 scopes today.**
  - Real-looking work: run outputs and a venv in `game-bot/feat-fxdiff`, data in `self-improvement/devtools-scout`, `trials/` in `memdoor-local`, drafts data in `awm/redux-trust`.
  - Rebuildable output: Rust `target/`, `*.egg-info`, `homelab/.collections/`, a Claude lock file.
  - A false positive: a folder that holds only `__pycache__` in `awm/redux-harness`.
- **The `.awm/` folder.**

  | File | Written by | Recoverable from |
  |---|---|---|
  | `context.md` | scaffold, then agents by hand | nowhere |
  | `history.md` | journal render | the journal |
  | `mcp-opencode.json` | scaffold and heal | regenerated. Obsolete, because R135 drops OpenCode |
  | `data` link | data provisioning | regenerated |
  | `data-mounts`, `data-checkout-failed` | DVC hooks | `data-mounts` is a local choice and is lost |
  | `gap-inventory.md` (9 redux lanes) | an agent by hand | nowhere |

  The check skips all of `.awm/`, including `context.md`, the one file there that nothing else holds.

## Differences from the design

- R44 item 3 names no exemptions. The code exempts DVC pins and caches.
- R44 item 4 blocks unpushed commits even when the delete keeps the branch. The code checks them only when the branch goes too (`scopes.py:414`).
- A forced delete must list what it discarded. `complete` with cleanup drops that list (`scopes.py:1322`).
- `scopes.md` § Gap for R44 still says ignored files are missed.
- `AGENTS.md` says `.awm/data -> ../data`. Every link on mizar points to the shared folder.

## Decisions

- 2026-11-03: A delete fails only where work is lost. Ignored files never block it, and the ignored-file check goes. An uncommitted pin or `dvc status` drift blocks it, the same as uncommitted git work (`scopes.md` R44 item 3). Tony: "basic motivation is that scope delete should fail where work is lost. gitignored items are marked as loosable. secrets are out of the concern for scopes dvc handles data".
- 2026-11-03: `.awm/` goes. The brief moves into the journal, hand-written notes into `docs/` or the journal, and the rest is dropped (`scopes.md` R142). Tony: "agree".
- 2026-11-03: The brief orients an agent and points it at the right places. `debrief` keeps it current, it repeats nothing the journal holds, and its size is capped (`scopes.md` R143). Tony: "lets also improve the maintenance on context.md -> debrief skill keeps it up to date? this should not be redundant with journals the size should also be capped it shoul get the agent going and looking at the right places. it doesnt need to be comprehensive".

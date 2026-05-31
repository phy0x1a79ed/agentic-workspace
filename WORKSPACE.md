# Workspace Reference

Shared context for all agents in this workspace.

## Workspace Layout

| Path | Purpose |
|------|---------|
| `awm/` | AWM service package (Python) + skills catalog |
| `data/` | Shared data (per-project; raw, staged, outputs) |
| `projects/` | Project bare repos + git worktrees (agents work here) |

### Per-Scope Layout

Agents land directly in the git worktree. All AWM metadata lives in a `.awm/` dotdir inside:

```
projects/{project}/
  .bare/                         # bare git repo
  {scope}/                       # git worktree — agent CWD
    .awm/                        # AWM metadata (gitignored)
      context.md                 # scope instructions
      history.md                 # auto-generated: open/resolved session history
      artifacts.md               # auto-generated: project artifact index
      data -> ../../../data/{project}/  # symlink to shared project data
      skills -> ../../../awm/skills/    # symlink to skill catalog
    [code files...]              # the actual repo content
```

Scopes access project data via `.awm/data/`. All scopes in the same project share the same data directory.

## Scope Lifecycle

1. **Create**: `scope_create` sets up a git worktree on `feat/{scope}` with `.awm/` metadata.
2. **Startup**: Agent reads `.awm/context.md`, runs `awm_refresh`, reads `history.md` + `artifacts.md`.
3. **Work**: Code in the current directory. Data at `.awm/data/`. Skills at `.awm/skills/`.
4. **Debrief**: User says "debrief" — agent follows `skills_get path="awm/debrief.md"`.
5. **Complete**: `scope_complete` updates DB status, optionally merges branch.

## Skills System

Skills are dynamic protocols that improve with use:

- **AWM skills** (`skills/awm/`): workspace procedures that drive the MCP tool surface (create-project, create-scope, debrief, skill-update)
- **Tool guides** (`skills/tools/`): external-tool references (git, mamba, dependencies, mcp, metasmith, plotly)
- Skills have frontmatter with `tags`, `requires`, and `scope` for search and hierarchy
- `skills_search` combines keyword + semantic search (sentence-transformers embeddings)
- **Session logs** can include execution traces attached to skills — log what happened, outcome, deviations, and improvement suggestions via `session_log`
- A dedicated `awm/skill-improvement` scope periodically reads session logs and revises skills

## Git Model

Each project uses a **bare repo** at `projects/{project}/.bare/` with worktrees per scope.

- Branch naming: `feat/{scope}`
- PRs created from feature branches
- See `skills/tools/git.md` for details

## Python Environment Rules

System Python is externally managed (PEP 668) — `pip install` is blocked.

**Do NOT use:** `python`, `python3`, `pip`, or `pip3` directly.
**Do NOT use:** `conda activate` or `mamba activate` (requires interactive shell init).

**Always use:**
```bash
mamba run -n <project-env> python script.py
mamba run -n <project-env> pip install <package>
```

## Agent Rules

1. **Raw data is immutable** — never modify files in `data/{project}/raw/`.
2. **Write outputs to `.awm/data/`** — shared across all scopes in the project.
3. **Don't edit `.awm/history.md` or `.awm/artifacts.md`** — these are auto-generated. Use MCP tools.
4. **Follow the debrief skill** when ending a session — log sessions, register artifacts, reflect.
5. **Search skills first** — use `skills_search` before starting unfamiliar workflows.

## Service Hub

`awm.exposed:app` (port 7820) is a routing layer. Most requests are served by its in-process routers (`/rooms`, `/peer`, `/voice`, …). A few path prefixes are *registered* by external services at runtime; matched requests are forwarded to those services with hub-as-peer auth.

### When to make a `svc-*` scope

Use a `svc-*` worktree when a stripe needs to **own** a path prefix end-to-end: ship its own routes, run as its own process, iterate without rebuilding the monolith. PTT V2 (audio + WS + STT) is the first customer. Pure shared-library refactors stay in `awm/`.

### Registration lifecycle

```bash
# in the svc-* worktree, with the service running on a chosen port:
awm hub register --name <svc> --prefix </owned-path> --url http://127.0.0.1:<port>
```

`register` POSTs to `/hub/register`, then holds a WS lease at `/hub/lease/<id>` until you Ctrl-C. Lease close → eviction within the next event-loop tick. No file-based config, no heartbeat tuning. Re-running re-registers from scratch.

Other commands:
- `awm hub list` — show current registrations.
- `awm hub deregister <name>` — admin force-evict.
- `awm hub trust-self` — install the local auth token at `$AWM_DIR/peers/<self>.token` so the hub's forwarded requests pass `require_peer_bearer` on the service side. Run once per node.

### Auth model

Hub → service is degenerate peer auth. The hub injects `Authorization: Bearer <local-auth.token>` + `X-Awm-From: <self-peer-id>` on every forwarded request; the user's bearer (`Authorization` header / `awm_session` cookie) is stripped. `X-Awm-As` is preserved verbatim. Services gate routes with `from awm.middleware_auth import require_peer_bearer` — one import, no new bearer concept.

### What `comp-*` and frontend slices need to know

**Nothing.** The hub IS `awm.exposed:app` on :7820; no new origin, no new port. With an empty registry the behavior is byte-identical to a hub-less awm.

### Demo

`awm/demos/echo_svc.py` is a 60-line FastAPI smoke test — copy it as the starting point for a real `svc-*`.

### Constraints

- **Never run two hubs on one node.** Both would bind :7820 and one would fail.
- The hub control plane (`/hub/*`) is exempt from forwarding even if a prefix would shadow it — the lease socket has to stay reachable.

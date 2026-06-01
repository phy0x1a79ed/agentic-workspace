# AWM Workspace

*Structural orientation for any agent operating in a scope worktree of this AWM workspace. Documents the workspace's paths (`.awm/`, `data/`, `skills/`), MCP tool catalog, project layout, scope lifecycle, and the startup ritual that every scope agent inherits. Loaded into scope-agent context via the harness's native mechanism — Claude Code Reads this file at session start per its global instructions (`~/.claude/CLAUDE.md`); OpenCode auto-injects it via the per-scope `mcp-opencode.json` `instructions` array. **Do not merge into AGENTS.md** (awm-internal, narrower audience) **or README.md** (human setup, different audience) — keeping the three files audience-pure is what lets each one stay legible.*

Context is assembled general → specific: this file first, then the cwd-local `AGENTS.md` (the project's hand-maintained brief), then `.awm/context.md` (the scope's per-task ritual).

## Workspace Layout

| Path | Purpose |
|------|---------|
| `WORKSPACE.md` | This file — loaded by every scope agent via harness-native mechanism (CC Reads per global instructions; OC auto-injects via per-scope opencode config) |
| `AGENTS.md` | AWM-internal architecture (loaded when cwd has it locally — CC Reads via walk-up, OC walks it natively) |
| `README.md` | Human setup/usage guide (never auto-injected) |
| `awm/` | AWM service package (Python) + skills catalog |
| `data/` | Shared data (per-project; raw, staged, outputs) |
| `projects/` | Project bare repos + git worktrees (agents work here) |
| `.awm/` | Workspace runtime state (`spawn-mcp.json`, `mcp-opencode.json`, peer tokens) |
| `.mcp.json` | Canonical MCP server registry — fans out via the exporter framework |

### Per-Scope Layout

Agents land directly in the git worktree. All AWM metadata lives in a `.awm/` dotdir inside:

```
projects/{project}/
  .bare/                         # bare git repo
  {scope}/                       # git worktree — agent CWD
    .awm/                        # AWM metadata (gitignored)
      context.md                 # scope instructions (auto-loaded)
      history.md                 # auto-generated: open/resolved session history
      artifacts.md               # auto-generated: project artifact index
      data -> ../../../data/{project}/  # symlink to shared project data
      skills -> ../../../awm/skills/    # symlink to skill catalog
    [code files...]              # the actual repo content
```

Scopes access project data via `.awm/data/`. All scopes in the same project share the same data directory.

## Existing Projects

```
projects/
  _vagrant/              # sentinel: per-user vagrant-scope handlers
  awm/                   # AWM itself (dev, web-ui, comp-*, infra-*, voice, sentry, …)
  container_builds/      # apptainer image recipes
  cyanoverse/            # cyanobacteria genomics figures + analyses
  drawio/                # diagrams + poster integration
  market_monitor/        # trading data pipelines
  metasmith/             # metasmith dev (caching, cancellation, hints, mcp, …)
  metasmith-libraries/   # per-pipeline libraries (eukaryotic-assembly, fabfos, phyloflash, …)
  mitacs-purify/         # bioreactor work
  research/              # biofilms, ecological-modelling, functional-decomposition
  scadc/                 # figures + analyses for the SCADC paper
  scratch/               # one-off sandboxes (endfield, minecraft-turtles, network_debug)
  self-improvement/      # factorio-learning-environment, opencode
  spanish-lakes/         # spanish-lakes metagenomics
  synclust/              # synclust dev
  threejs-scene-manager/ # scene manager dev
  tools/                 # misc tooling
  vpn_bounce/            # vpn relay experiments
```

Each project has one or more scope worktrees under it; `awm scope list --project <p>` enumerates them live.

## Startup Ritual

Every scope agent runs this on session start (the `.awm/context.md` for newly-created scopes embeds the boilerplate; agents in long-lived scopes can re-run it any time to refresh):

1. `mcp__awm__awm_refresh project=<p> scope=<s>` — re-renders `.awm/history.md` and `.awm/artifacts.md` from the DB.
2. Read `.awm/history.md` — open + resolved session log for this scope and its siblings.
3. Read `.awm/artifacts.md` — registered artifacts (data files, model outputs, figures) from sibling scopes.
4. `mcp__awm__skills_search query="<your task description>"` — finds the relevant procedural skill before you start.
5. `mcp__awm__inbox_search status=unread scope=scope:<p>/<s>` (and optionally `scope=workspace`) — anything addressed to you or the workspace that's waiting.

`.awm/history.md` and `.awm/artifacts.md` are auto-generated. Never edit them by hand — use `awm_refresh`, `session_log`, and `artifact_register` MCP tools.

## MCP Tools

The MCP server (`awm-mcp`) is registered at `<workspace>/.mcp.json` and auto-discovered by Claude Code, OpenCode, and other MCP clients. The tool surface, by category:

| Category | Tools |
|---|---|
| Skills | `skills_list`, `skills_get`, `skills_search`, `skills_sync` |
| Sessions | `session_log`, `session_list`, `session_get`, `session_search`, `session_resolve` |
| Scopes | `scope_create`, `scope_list`, `scope_complete`, `scope_delete` |
| Projects | `project_create`, `project_list` |
| Artifacts | `artifact_register`, `artifact_search`, `artifact_delete`, `artifacts_sync` |
| Locks | `lock_acquire`, `lock_release`, `lock_list`, `lock_heartbeat` |
| Messaging | `inbox_send`, `inbox_search`, `inbox_fetch`, `inbox_mark_read`, `inbox_recipients` |
| Rooms | `room_create`, `room_list`, `room_get`, `room_history`, `room_search`, `room_post`, `room_invite`, `room_remove`, `room_close`, `room_archive`, `room_agents` |
| Peers | `peers_list`, `peer_ping` |
| Lifecycle | `awm_status`, `awm_restart`, `awm_refresh`, `agent_control` |

Each tool has a JSON Schema accessible via your MCP client. New tools land here automatically when `awm-mcp` reloads.

## Skills Discovery

Skills are dynamic protocols that improve with use:

- **AWM skills** (`skills/awm/`): workspace procedures that drive the MCP tool surface (create-project, create-scope, debrief, skill-update, harness-setup, …)
- **Tool guides** (`skills/tools/`): external-tool references (git, mamba, dependencies, mcp, metasmith, plotly, chrome-devtools)
- Skills have frontmatter with `tags`, `requires`, and `scope` for search and hierarchy.
- `skills_search` combines keyword + semantic search (sentence-transformers embeddings).
- Session logs can include execution traces attached to skills — log what happened, outcome, deviations, and improvement suggestions via `session_log`.
- A dedicated `awm/skill-improvement` scope periodically reads session logs and revises skills.

When you don't know the procedure for a verb (e.g. "create scope", "debrief", "register artifact"), **search skills before guessing** — the answer is almost always already written.

## Scope Lifecycle

1. **Create**: `scope_create` sets up a git worktree on `feat/{scope}` with `.awm/` metadata.
2. **Startup**: Agent reads `.awm/context.md` (auto-injected), runs the Startup Ritual above.
3. **Work**: Code in the current directory. Data at `.awm/data/`. Skills at `.awm/skills/`.
4. **Debrief**: User says "debrief" — agent follows `skills_get path="awm/debrief.md"`.
5. **Complete**: `scope_complete` updates DB status, optionally merges branch.

## Scope Naming Convention

New scopes use a prefix family to signal what kind of work they own. Names are flat (slashes are rejected — see `awm/services/_validation.py`), so the family is encoded as a hyphen-prefix.

| Prefix | Family | What it owns |
|--------|--------|-------------|
| `comp-*` | component | A single frontend component + its fixtures (one slug under `/dev/components/`). |
| `svc-*`  | service   | A backend service contract — endpoints, models, the Pydantic surface for an area. |
| `feat-*` | feature   | End-to-end integration that wires components, services, and external engines together (e.g. `feat-stt`, `feat-rooms`). |
| `infra-*`| infrastructure | Cross-cutting toolchain that other scopes consume — codegen, dev surfaces, test runners. |

Older scopes (`dev`, `sentry`, `vagrant-*`, `voice`, `web-ui`) predate this convention and keep their flat keyword names. The prefix family applies to scopes created from this point forward.

For component+backend stripes packaged as workspace packages (one `packages/<name>/` per stripe, auto-discovered by the hub at sandbox start), see § *Developing a vertical stripe* in `projects/awm/dev/AGENTS.md`. New stripes prefer that flow over the inline `comp-*` + `svc-*` registration.

## Git Model

Each project uses a **bare repo** at `projects/{project}/.bare/` with worktrees per scope.

- Branch naming: `feat/{scope}` (or flat keyword for legacy scopes).
- PRs created from feature branches into `main` / `release` as appropriate.
- See `skills_get path="tools/git.md"` for the worktree-bare flow in detail.

## CLI Quick Reference

`awm <command> --help` for full options on any of these. The MCP tools above are usually more ergonomic from inside an agent — the CLI is for shell-level work.

| Command | Purpose |
|---|---|
| `awm status` / `awm serve` / `awm stop` / `awm restart` | Core lifecycle |
| `awm project create <name>` | Create a project (optionally `--clone` / `--fork`) |
| `awm scope create <p> <s>` / `awm scope list` / `awm scope complete <p> <s>` | Scope worktree management |
| `awm scope heal [--dry-run]` | Cleanup pass: enforce tier-3 = `.awm/` only across active scopes |
| `awm session log <p> <s> --summary ... --decision ...` | Record a session entry |
| `awm lock acquire <path> --holder <id>` / `awm lock release / list / reap` | File / folder locks (heartbeat every 30s) |
| `awm skill list / search / get / reindex` | Skill catalog |
| `awm hub register / list / deregister` | Service Hub control plane (awm-internal — see AGENTS.md) |
| `awm context emit --cwd <path>` | Render the 3-tier context as XML blocks (utility for awm tooling that bundles context into spawned sessions; no harness hook calls it) |
| `awm peer add / list / ping / whoami` | Federation setup (see README.md) |

## Agent Rules

1. **Raw data is immutable** — never modify files in `data/{project}/raw/`.
2. **Write outputs to `.awm/data/`** — shared across all scopes in the project.
3. **Don't edit `.awm/history.md` or `.awm/artifacts.md`** — auto-generated. Use MCP tools.
4. **Follow the debrief skill** when ending a session — log sessions, register artifacts, reflect.
5. **Search skills first** — use `skills_search` before starting unfamiliar workflows.

## Python Environment Rules

System Python is externally managed (PEP 668) — `pip install` is blocked.

**Do NOT use:** `python`, `python3`, `pip`, `pip3` directly.
**Do NOT use:** `conda activate` / `mamba activate` (requires interactive shell init).

**Always use:**

```bash
mamba run -n <project-env> python script.py
mamba run -n <project-env> pip install <package>
```

For AWM itself: `mamba run -n awm <cmd>` (the `awm` env, created by `./setup.sh`).

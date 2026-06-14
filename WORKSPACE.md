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
| `.awm/` | Workspace runtime state (`spawn-mcp.json`, `mcp-opencode.json`, etc.) |
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
  odysseus/              # odysseus fork (https://github.com/phy0x1a79ed/odysseus)
  research/              # biofilms, ecological-modelling, functional-decomposition
  scadc/                 # figures + analyses for the SCADC paper
  scratch/               # one-off sandboxes (endfield, minecraft-turtles, network_debug)
  self-improvement/      # factorio-learning-environment, opencode
  spanish-lakes/         # spanish-lakes metagenomics
  synclust/              # synclust dev
  threejs-scene-manager/ # scene manager dev
  vpn_bounce/            # vpn relay experiments
```

Each project has one or more scope worktrees under it; `awm scope list --project <p>` enumerates them live.

## Startup Ritual

Every scope agent runs this on session start (the `.awm/context.md` for newly-created scopes embeds the boilerplate; agents in long-lived scopes can re-run it any time to refresh):

1. `mcp__awm__scope_refresh project=<p> scope=<s>` — re-renders `.awm/history.md` and `.awm/artifacts.md` from the DB.
2. Read `.awm/history.md` — open + resolved session log for this scope and its siblings.
3. Read `.awm/artifacts.md` — registered artifacts (data files, model outputs, figures) from sibling scopes.
4. `mcp__awm__skill_search query="<your task description>"` — finds the relevant procedural skill before you start.
5. `mcp__awm__scope_fetch scope=<s> kind=message` (and optionally the `workspace` channel) — anything addressed to you or the workspace that's waiting.

`.awm/history.md` and `.awm/artifacts.md` are auto-generated. Never edit them by hand — use `scope_refresh`, `scope_post`, and `artifact_register` MCP tools.

## MCP Tools

The MCP server (`awm-mcp`) is registered at `<workspace>/.mcp.json` and auto-discovered by Claude Code, OpenCode, and other MCP clients. The surface is **projected live** from the registered feature services — your MCP client lists the current tools with their JSON Schemas, so that listing (not a table here) is the source of truth. Tool names follow a `<domain>_<verb>` convention; the families:

- **Scopes & identity** (`scope_*`, `ref_resolve`) — create / search / complete / delete a scope, sync/repair its branch, `scope_refresh` to rebuild its `.awm/` indexes, and the natural-key identity reads (`scope_resolve`, `scope_ensure`, `ref_resolve`).
- **Projects** (`project_*`) — create / search / ensure a project.
- **Scope channel** (`scope_post`, `scope_fetch`, `scope_subscribe`, `scope_unsubscribe`) — a scope *is* the channel: post messages/journal entries, fetch or search them (`scope_fetch ... order='desc'` for the last N), subscribe guests.
- **Agents** (`agent_*`, `slash_catalog`) — spawn / list / stop / kill agent sessions, `agent_log` to tail one, dispatch slash commands, subscribe to an agent's act stream.
- **Artifacts** (`artifact_*`) — register / search / get / delete / sync registered outputs.
- **Skills** (`skill_search`, `skill_get`, `skill_sync`) — discover and read procedural skills.
- **Discord** (`discord_*`) and **lifecycle** (`awm_status`, `awm_restart`, `awm_mcp_sync`).

New tools appear in the listing automatically as services register — no restart, nothing to mirror here.

## Skills Discovery

Skills are dynamic protocols that improve with use:

- **AWM skills** (`skills/awm/`): workspace procedures that drive the MCP tool surface (create-project, create-scope, debrief, skill-update, harness-setup, …)
- **Tool guides** (`skills/tools/`): external-tool references (git, mamba, dependencies, mcp, metasmith, plotly, chrome-devtools)
- Skills have frontmatter with `tags`, `requires`, and `scope` for search and hierarchy.
- `skill_search` combines keyword + semantic search (sentence-transformers embeddings).
- Session logs can include execution traces attached to skills — log what happened, outcome, deviations, and improvement suggestions via `scope_post` (kind=journal).
- A dedicated `awm/skill-improvement` scope periodically reads session logs and revises skills.

When you don't know the procedure for a verb (e.g. "create scope", "debrief", "register artifact"), **search skills before guessing** — the answer is almost always already written.

## Scope Lifecycle

1. **Create**: `scope_create` sets up a git worktree on `feat/{scope}` with `.awm/` metadata.
2. **Startup**: Agent reads `.awm/context.md` (auto-injected), runs the Startup Ritual above.
3. **Work**: Code in the current directory. Data at `.awm/data/`. Skills at `.awm/skills/`.
4. **Debrief**: User says "debrief" — agent follows `skill_get path="awm/debrief.md"`.
5. **Complete**: `scope_complete` updates DB status, optionally merges branch.

## Scope Naming Convention

New scopes use a prefix family to signal what kind of work they own. Names are flat (slashes are rejected — see `awm/services/scopes/awm/scopes/_validation.py`), so the family is encoded as a hyphen-prefix.

| Prefix | Family | What it owns |
|--------|--------|-------------|
| `comp-*` | component | Cross-cutting work on a single shared frontend component (a deeper rework than a normal PR). The component itself lives in `packages/components/<name>/` regardless of which scope is editing it. |
| `svc-*`  | service   | Cross-cutting work on a single long-running backend service. The service itself lives in `awm/services/<name>/`. |
| `feat-*` | feature   | Multi-package composition that wires components, services, and pages together (e.g. `feat-stt`, `feat-rooms`). |
| `infra-*`| infrastructure | Cross-cutting toolchain that other scopes consume — codegen, dev surfaces, test runners, the service hub itself. |

Older scopes (`dev`, `sentry`, `vagrant-*`, `voice`, `web-ui`) predate this convention and keep their flat keyword names. The prefix family applies to scopes created from this point forward.

For the day-to-day workflow of authoring/iterating on a package — what files you write, the shadow flow — see § *Developing a package* in the awm-internal AGENTS.md (auto-loaded inside any `projects/awm/*` scope).

## Git Model

Each project uses a **bare repo** at `projects/{project}/.bare/` with worktrees per scope.

- Branch naming: `feat/{scope}` (or flat keyword for legacy scopes).
- PRs created from feature branches into `main` / `release` as appropriate.
- See `skill_get path="tools/git.md"` for the worktree-bare flow in detail.

## CLI Quick Reference

`awm <command> --help` for full options on any of these. The MCP tools above are usually more ergonomic from inside an agent — the CLI is for shell-level work.

| Command | Purpose |
|---|---|
| `awm gateway init` / `awm gateway status` / `awm gateway serve` / `awm gateway stop` / `awm gateway restart` | Core lifecycle |
| `awm project create <name>` | Create a project (optionally `--clone` / `--fork`) |
| `awm scope create <p> <s>` / `awm scope list` / `awm scope complete <p> <s>` | Scope worktree management |
| `awm scope heal [--dry-run]` | Cleanup pass: enforce tier-3 = `.awm/` only across active scopes |
| `awm session log <p> <s> --summary ... --decision ...` | Record a session entry |
| `awm skill list / search / get / reindex` | Skill catalog |
| `awm gateway register / list / deregister` | Service Hub control plane (awm-internal — see AGENTS.md) |

## Agent Rules

1. **Raw data is immutable** — never modify files in `data/{project}/raw/`.
2. **Write outputs to `.awm/data/`** — shared across all scopes in the project.
3. **Don't edit `.awm/history.md` or `.awm/artifacts.md`** — auto-generated. Use MCP tools.
4. **Follow the debrief skill** when ending a session — log sessions, register artifacts, reflect.
5. **Search skills first** — use `skill_search` before starting unfamiliar workflows.

## Python Environment Rules

System Python is externally managed (PEP 668) — `pip install` is blocked.

**Do NOT use:** `python`, `python3`, `pip`, `pip3` directly.
**Do NOT use:** `conda activate` / `mamba activate` (requires interactive shell init).

**Always use:**

```bash
mamba run -n <project-env> python script.py
mamba run -n <project-env> pip install <package>
```

For AWM itself: `mamba run -n awm <cmd>` (the `awm` env, created by `awm/gateway/setup.sh`).

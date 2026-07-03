# AWM Workspace

*Structural orientation for any agent operating in a scope worktree of this AWM workspace — the workspace's paths, MCP tool catalog, project layout, scope lifecycle, and the startup ritual every scope agent inherits. Loaded into scope-agent context via the harness's native mechanism: Claude Code Reads this file at session start per its global instructions (`~/.claude/CLAUDE.md`); OpenCode auto-injects it via the per-scope `mcp-opencode.json` `instructions` array.*

Context is assembled general → specific: this file first, then the cwd-local `AGENTS.md` (the project's hand-maintained brief), then `.awm/context.md` (the scope's per-task ritual).

## Workspace Layout

| Path | Purpose |
|------|---------|
| `WORKSPACE.md` | This file — loaded by every scope agent via harness-native mechanism (CC Reads per global instructions; OC auto-injects via per-scope opencode config) |
| `AGENTS.md` | AWM-internal architecture (loaded when cwd has it locally — CC Reads via walk-up, OC walks it natively) |
| `README.md` | Human setup/usage guide (never auto-injected) |
| `awm/` | AWM service package (Python) |
| `skills/` | Reference protocol docs (read-only; the skills *service* is retired) |
| `data/` | Shared data (per-project; raw, staged, outputs) |
| `projects/` | Project bare repos + git worktrees (scope agents work here) |
| `tasks/` | Per-task workspace units — DAG node execution sandboxes (gitignored) |
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
      artifacts.md               # auto-generated: pointer on discovering/reusing sibling scopes' outputs
      data -> ../../../data/{project}/  # symlink to shared project data
      skills -> ../../../awm/skills/    # symlink to skill catalog
    [code files...]              # the actual repo content
```

Scopes access project data via `.awm/data/`. All scopes in the same project share the same data directory.

## Existing Projects

```
projects/
  _vagrant/              # sentinel: per-user vagrant-scope handlers
  awm/                   # AWM itself (dev, feat-dag, feat-gamebot, web-*, svc-*, comp-*, infra-*, …)
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

1. `scope(verb="refresh", args={project:<p>, scope:<s>})` — re-renders `.awm/history.md` (session log, from the DB) and `.awm/artifacts.md` (the artifact-discovery pointer).
2. Read `.awm/history.md` — open + resolved session log for this scope and its siblings.
3. Skim `.awm/artifacts.md` — how to discover and reuse sibling scopes' outputs (figures, datasets, reports, models, scripts). It's a bounded pointer, not a list; `artifact_search` returns the live matches when you need one.
4. `scope(verb="fetch", args={scope:<s>, kind:"message"})` (and optionally the `workspace` channel) — anything addressed to you or the workspace that's waiting.

`.awm/history.md` and `.awm/artifacts.md` are auto-generated. Never edit them by hand — use the `scope` domain's `refresh`/`post` verbs and `artifact`'s `register` verb (`scope(verb="refresh", …)`, etc.).

## MCP Tools

The MCP server (`awm-mcp`) is registered at `<workspace>/.mcp.json` and auto-discovered by Claude Code, OpenCode, and other MCP clients. The surface is **projected live** from whatever feature services are currently registered and **collapsed by domain**: instead of one tool per `<domain>_<verb>` (dozens of them), your client sees **one generic tool per domain** — `scope`, `project`, `agent`, `artifact`, `services`, … — each called with `{ "verb": "<name>", "args": { … } }`. This keeps the tool surface tiny for clients that can't defer schemas (spawned agents, OpenCode).

**The catalog is self-describing — discover it, don't memorize it.** This file deliberately does **not** enumerate the domains or their verbs. The set grows every time a service registers (`social`, `2fa`, `mic`, `vpn`, `ssh`, `reflection`, `writing`, … all arrived this way), so any list written here would only drift and go stale. Find what's actually available at runtime instead — two moves:

1. **Which domains exist** — the domain tools your MCP client exposes *are* the live catalog. In clients that defer tool schemas a domain may show as a bare name until loaded, so surface one by keyword with `ToolSearch` (e.g. search `social`, `2fa`), or list the running services with `services(verb="list")` — each service is a domain.
2. **What a domain can do** — call it with `verb="describe"` (optionally `args={"verb":"<name>"}` to narrow to one verb) for its verbs and full parameter schemas, answered instantly from the catalog with no service round-trip. Example: `scope(verb="describe")` → `create / search / complete / refresh / post / fetch / …`; then `scope(verb="search", args={"query":"…"})` runs it. `describe` is a reserved verb on every domain.

So the reflex when a task *looks* like it needs a human — send a message, approve a login/2FA, capture audio, bounce a VPN — is to `describe` a plausible domain or `ToolSearch` first: the capability is often already a tool (see `~/.claude/CLAUDE.md` § *Reach for a tool before handing work back*). Server-side, a placed agent's mode restricts which verbs it may call regardless of harness (see AGENTS.md), so a disallowed verb is rejected, not silently honored.

One domain is worth singling out because it changes *how you search*, not just what you can do: **`graphify`** — an AST knowledge graph of the awm source tree (`find` label→`file:line`, `refs` callers/callees/importers, `query` NL traversal, `path`, `affected` blast-radius). Reach for it **before dispatching an Explore agent** on any "where is X / what calls or imports Y / impact of changing Z" question on the awm tree; it answers structurally and mostly in-process. Caveat: it indexes the **deployed/release** tree, not your uncommitted worktree changes — use Explore for code you just wrote and for non-awm projects.

The **CLI and HTTP surfaces stay expanded** — `awm scope create`, `POST /invoke {name:"scope_create"}`, one command/route per verb — so only the MCP projection collapses; see the CLI note below.

## Skills

The end-of-session **debrief** is a native Claude Code skill (`~/.claude/skills/debrief/`): say "debrief" and the agent runs it — commit, journal (`scope_post kind=journal`), reconcile artifacts, refresh. No MCP lookup needed.

Other procedural references (the `create-project` / `create-scope` / `harness-setup` writeups and tool guides for git, mamba, mcp, metasmith, plotly, chrome-devtools, threejs) still live on disk under `.awm/skills/` and are Read-able when relevant. The skills *service* (the `skill_search` / `skill_get` / `skill_sync` MCP tools + embeddings search) is **retired/disabled** — these files are reference-only now, not searchable through MCP.

Session execution traces — what happened, outcome, deviations, suggestions — go in the journal via `scope_post` (kind=journal); the debrief skill stamps `skill_path:"awm/debrief.md"` so `history.md` groups them.

## Scope Lifecycle

1. **Create**: `scope_create` sets up a git worktree on `feat/{scope}` with `.awm/` metadata.
2. **Startup**: Agent reads `.awm/context.md` (auto-injected), runs the Startup Ritual above.
3. **Work**: Code in the current directory. Data at `.awm/data/`. Skills at `.awm/skills/`.
4. **Debrief**: User says "debrief" — agent runs the native `debrief` skill (`~/.claude/skills/debrief/`).
5. **Complete**: `scope_complete` updates DB status, optionally merges branch.

## Scope Naming Convention

New scopes use a prefix family to signal what kind of work they own. Names are flat (slashes are rejected — see `awm/services/scopes/awm/scopes/_validation.py`), so the family is encoded as a hyphen-prefix.

| Prefix | Family | What it owns |
|--------|--------|-------------|
| `comp-*` | component | Cross-cutting work on a single shared frontend component (a deeper rework than a normal PR). The component itself lives in `awm/ui_components/<name>/` regardless of which scope is editing it. |
| `svc-*`  | service   | Cross-cutting work on a single long-running backend service. The service itself lives in `awm/services/<name>/`. |
| `feat-*` | feature   | Multi-package composition that wires components, services, and pages together (e.g. `feat-stt`, `feat-rooms`). |
| `infra-*`| infrastructure | Cross-cutting toolchain that other scopes consume — codegen, dev surfaces, test runners, the service hub itself. |

Older scopes (`dev`, `sentry`, `vagrant-*`, `voice`, `web-ui`) predate this convention and keep their flat keyword names. The prefix family applies to scopes created from this point forward.

**Composition scopes.** A couple of `feat-*` scopes are *standing* composition scopes — one per feature family — that own the cross-service wiring + integration playbooks for that family and run their **own isolated dev sandbox** (port pinned via a gitignored `awm/gateway/dev/.env`), factoring `svc-*`/`comp-*` units out as they stabilize:

- **`feat-dag`** (`:7861`) — conversational agents + voice (`stt`/`tts`) + web-ui chat + **DAG orchestration** (agents/orchestrator/workspace services, `@awm/chat`, `@awm/dag-graph`).
- **`feat-gamebot`** (`:7871`) — LLM-driven web-game bots (realm/effector/agent-runner/timer services).

`dev` is **not** a feature scope — it is the **release-staging / promotion** worktree (`feat → dev → release`, prod deploys) and runs the shared seeded sandbox at `:7821`.

### Hubs & peripherals (scatter / gather)

A **hub** scope integrates work from a set of **peripheral** feature scopes via two batch git operations exposed by the scopes service — **gather** (fan-in: merge each peripheral's `feat/<p>` into the hub branch) and **scatter** (fan-out: merge the hub branch back into each peripheral). Both are **local-only** (no push) and **stateless** — the peripheral list is passed explicitly; this table *is* the convention they read from. Drive them with the `scatter-gather` Claude Code skill, or call `scope(verb="gather"|"scatter", args={project, hub, peripherals})` directly.

| Hub | Branch | Peripherals (seed — edit as the family changes) |
|-----|--------|-------------------------------------------------|
| `feat-dag` | `feat/feat-dag` | `svc-agents`, `svc-orchestrator`, `svc-events`, `web-stt`, `web-tts`, `web-ui` |
| `feat-gamebot` | `feat/feat-gamebot` | `svc-effector`, `svc-events`, `rlm-browser`, `rlm-factorio` |
| `dev` | `dev` | all promotable scopes (the `svc-*`, `web-*`, `rlm-*` set) |

This is the canonical, shared copy; each hub may mirror its own row into its `.awm/context.md` (gitignored, so local-only) for a hub agent to find it without walking up here.

For the day-to-day workflow of authoring/iterating on a service, page, or component — what files you write, the build + shadow flow — see `README.md` § *Authoring a service* / § *Authoring a page*; the internal architecture behind it is in the awm-internal `AGENTS.md` (auto-loaded inside any `projects/awm/*` scope).

## Git Model

Each project uses a **bare repo** at `projects/{project}/.bare/` with worktrees per scope.

- Branch naming: `feat/{scope}` (or flat keyword for legacy scopes).
- PRs created from feature branches into `main` / `release` as appropriate.
- See `.awm/skills/tools/git.md` for the worktree-bare flow in detail.

## CLI Quick Reference

`awm <command> --help` for full options on any of these. The MCP tools above are usually more ergonomic from inside an agent — the CLI is for shell-level work.

**The CLI mirrors the full expanded surface.** Beyond the gateway-control commands in the table below, the CLI generates one `awm <domain> <verb>` command per registered feature-service tool — `awm scope create`, `awm artifact register`, `awm agent list`, etc. — from the **same live catalog** the MCP surface reads (the default `GET /tools`, the per-verb projection), so the two never drift and a newly-registered service's verbs appear with no extra wiring. Note the asymmetry: the **MCP** projection collapses to one generic `{verb,args}` tool per domain (`GET /tools?view=domains`), but the **CLI/HTTP** surfaces stay fully expanded — one `awm <domain> <verb>` command and one `POST /invoke {name:"<domain>_<verb>"}` per verb — so shell ergonomics are unchanged. `awm <domain> --help` lists a domain's verbs; `awm <domain> <verb> --help` shows that tool's exact parameters straight from its `inputSchema` (all `--flag` options). When the gateway is down the CLI lists from a cached snapshot; when it's up it's live-accurate every invocation.

| Command | Purpose |
|---|---|
| `awm gateway init` / `awm gateway status` / `awm gateway serve` / `awm gateway stop` / `awm gateway restart` | Core lifecycle |
| `awm project create <name>` | Create a project (optionally `--clone` / `--fork`) |
| `awm scope create <p> <s>` / `awm scope list` / `awm scope complete <p> <s>` | Scope worktree management |
| `awm scope heal [--dry-run]` | Cleanup pass: enforce tier-3 = `.awm/` only across active scopes |
| `awm session log <p> <s> --summary ... --decision ...` | Record a session entry |
| `awm gateway register / list / deregister` | Service Hub control plane (awm-internal — see AGENTS.md) |

## Agent Rules

1. **Raw data is immutable** — never modify files in `data/{project}/raw/`.
2. **Write outputs to `.awm/data/`** — shared across all scopes in the project.
3. **Don't edit `.awm/history.md` or `.awm/artifacts.md`** — auto-generated. Use MCP tools.
4. **Run the `debrief` skill** when ending a session — commit, log the session, register artifacts, refresh.
5. **Check `.awm/skills/` for a procedure** before improvising an unfamiliar workflow — the writeups are on disk even though the search service is retired.

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

## What goes in this file

WORKSPACE.md is the structural orientation every scope agent inherits at session start, for any project in the workspace: the workspace layout and per-scope `.awm/` paths, the project map, how the MCP tool surface is projected and discovered (not a hand-maintained list), the startup ritual, scope lifecycle and naming conventions, the git model, and the workspace-wide agent + Python-environment rules. awm-internal architecture goes in `AGENTS.md`; human install/usage goes in `README.md`.

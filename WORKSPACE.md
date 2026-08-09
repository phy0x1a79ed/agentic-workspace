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
      data -> ../data            # compat symlink; the real data/ is repo content
      skills -> ../../../awm/skills/    # symlink to skill catalog
    [code files...]              # the actual repo content, including data/
```

### Data

**Data is versioned by the same commit that versions the code.** A DVC-backed
project keeps its data at `<scope>/data/<chunk>` and a tracked ~110-byte *pin*
beside it at `<scope>/data/<chunk>.dvc`. The bytes live once, in a
content-addressed cache shared by every scope and project on the machine
(`<workspace>/data/.dvc_cache`). `.awm/data` is a symlink to `../data` and
exists only so older call sites keep working.

There is one lever. A commit records your code and the exact data it was built
against, together; merging a branch brings its data with it. So:

- **To save data you wrote:** `dvc add data/<chunk>`, then commit the changed
  `.dvc` pin alongside your code. There is no data verb, no data branch, no
  promote.
- **To take a sibling's data:** merge their branch. A post-merge hook checks the
  files out for you.
- **Isolation is branch isolation.** Your pins are yours until someone merges
  them; a sibling scope cannot see or clobber your work in progress.

| Verb | What it does |
|---|---|
| `scope(verb="data_status", …)` | mode, the commit that pins the data, which chunks it pins, which are materialised, and whether the workspace still matches |
| `scope(verb="data_mount", …)` | choose which chunks materialise on disk here. Unmounted chunks stay pinned and backed up — this only decides what costs inodes |
| `scope(verb="data_gc", …)` | reclaim cache space. Dry run by default, and it needs *every* project whose data must survive: the cache is shared |
| `dvc(verb="sync"/"pull"/"coverage")` | the off-site half — see § *Off-site backup* |

Two mechanical facts, and only these two:

1. **Materialised files are read-only hardlinks into the shared cache.** Editing
   one in place would corrupt that object for every other scope and every
   historical commit that pins it. Write a new file, or `dvc unprotect <path>`
   first.
2. **Never run a bare `dvc gc`.** It collects against one worktree's view of a
   cache the whole workspace shares. Use `data_gc`, which makes you name what to
   keep.

Projects that have not been converted keep a plain `.awm/data` symlink to
`data/{project}/` — shared, unversioned, every scope writing the same files.
Nothing migrates them; a project gets wired the first time a scope is created or
healed after its checkout carries a tracked `.dvc/config`.

**Delete superseded data.** That is what versioning buys: an old version stays
reachable from the commit that pinned it, so you never need two live copies to
answer "which one is current?"

### Off-site backup

Two remotes cover this machine between them: **GitHub holds the code, chinook
holds the cache.** A daily timer pushes `data/.dvc_cache` to the chinook Globus
collection, append-only — nothing ever deletes there, which is what makes a
local `dvc gc` recoverable.

Nothing else is backed up. Work in a worktree that is neither committed-and-
pushed nor DVC-pinned — scratch directories, run outputs, `.awm/` state — has no
copy anywhere, deliberately. `dvc(verb="coverage")` lists exactly what that is
per scope; run it before you assume something survived.

## Finding Projects

**Deliberately not enumerated here.** Projects and their scopes are created, renamed, and
completed constantly — any list written into this file is stale within days, and keeping it
current is pure churn for every agent that edits it. Do NOT add a project map back. The same
rule that governs the MCP catalog below applies here: **discover it, don't memorize it.**

Enumerate live instead:

- `project(verb="search")` — every project (MCP); `awm project search [query]` from a shell.
- `scope(verb="search", args={project:<p>})` — a project's scopes, their branches and worktrees;
  `awm scope list --project <p>` from a shell.
- `ls projects/` — the on-disk truth: one directory per project, each holding a `.bare/` repo
  and one worktree directory per scope.

What a given project is *for* lives in that project's own worktree — its `AGENTS.md`,
`README.md`, and per-scope `.awm/context.md` — not in this file.

## Startup Ritual

Every scope agent runs this on session start (the `.awm/context.md` for newly-created scopes embeds the boilerplate; agents in long-lived scopes can re-run it any time to refresh):

1. `scope(verb="refresh", args={project:<p>, scope:<s>})` — re-renders `.awm/history.md` (session log, from the DB).
2. Read `.awm/history.md` — open + resolved session log for this scope and its siblings.
3. `scope(verb="fetch", args={scope:<s>, kind:"message"})` (and optionally the `workspace` channel) — anything addressed to you or the workspace that's waiting.

`.awm/history.md` is auto-generated. Never edit it by hand — use the `scope` domain's `refresh`/`post` verbs (`scope(verb="refresh", …)`, etc.).

## MCP Tools

The MCP server (`awm-mcp`) is registered at `<workspace>/.mcp.json` and auto-discovered by Claude Code, OpenCode, and other MCP clients. The surface is **projected live** from whatever feature services are currently registered and **collapsed by domain**: instead of one tool per `<domain>_<verb>` (dozens of them), your client sees **one generic tool per domain** — `scope`, `project`, `agent`, `services`, … — each called with `{ "verb": "<name>", "args": { … } }`. This keeps the tool surface tiny for clients that can't defer schemas (spawned agents, OpenCode).

**The catalog is self-describing — discover it, don't memorize it.** This file deliberately does **not** enumerate the domains or their verbs. The set grows every time a service registers (`social`, `2fa`, `mic`, `vpn`, `ssh`, `reflection`, `writing`, … all arrived this way), so any list written here would only drift and go stale. Find what's actually available at runtime instead — three moves:

1. **Which domains exist** — the domain tools your MCP client exposes *are* the live catalog. In clients that defer tool schemas a domain may show as a bare name until loaded, so surface one by keyword with `ToolSearch` (e.g. search `social`, `2fa`), or list the running services with `services(verb="list")` — each service is a domain.
2. **What a domain can do** — call it with `verb="describe"` (optionally `args={"verb":"<name>"}` to narrow to one verb) for its verbs and full parameter schemas, answered instantly from the catalog with no service round-trip. Example: `scope(verb="describe")` → `create / search / complete / refresh / post / fetch / …`; then `scope(verb="search", args={"query":"…"})` runs it. `describe` is a reserved verb on every domain.

3. **Which node it runs on** — the envelope's third key, `peer`, alongside `verb` and `args`. Every domain tool appears **once** no matter how many peers the fleet has; omitting `peer` uses that domain's default provider (this node normally, a singleton's owner for a singleton, the sole peer for a domain no local service provides), and `providersOf(tool="<domain>")` — a reserved top-level tool, mirrored on the CLI as `awm peer providers` — reports the valid values and which is the default. A misdirected `peer` is refused naming the options, never quietly run locally. See `FEDERATION.md` § *Cross-peer calls*.

So the reflex when a task *looks* like it needs a human — send a message, approve a login/2FA, capture audio, bounce a VPN — is to `describe` a plausible domain or `ToolSearch` first: the capability is often already a tool (see `~/.claude/CLAUDE.md` § *Reach for a tool before handing work back*). Server-side, a placed agent's mode restricts which verbs it may call regardless of harness (see AGENTS.md), so a disallowed verb is rejected, not silently honored.

One domain is worth singling out because it changes *how you search*, not just what you can do: **`graphify`** — an AST knowledge graph of the awm source tree (`find` label→`file:line`, `refs` callers/callees/importers, `query` NL traversal, `path`, `affected` blast-radius). Reach for it **before dispatching an Explore agent** on any "where is X / what calls or imports Y / impact of changing Z" question on the awm tree; it answers structurally and mostly in-process. Caveat: it indexes the **deployed/release** tree, not your uncommitted worktree changes — use Explore for code you just wrote and for non-awm projects.

Another domain changes *how you decide*, not how you search: **`precedence`** — a searchable archive of past **user-adjustment decisions** (the free-text triple *situation + decision-point + what-was-decided*), so you act on a preference the user already expressed instead of re-asking. Reach for it **before re-asking the user a preference-shaped question, and whenever the user corrects or overrides a choice you made**: `precedence(verb="search", args={…})` — query any subset of the triple (`context` / `question` / `decision`) or `query` for keywords; pass `explore=0` for the single most-trusted hit — then act on the stored decision rather than re-litigating it. When the user *does* make or adjust such a decision, contribute it back so the archive compounds: `precedence(verb="add")` for a new one, `verb="note"` / `verb="vote"` to amend or rate an existing entry. (Curation verbs live on CLI/HTTP only; `precedence(verb="describe")` gives exact schemas.)

A third changes *how you manage context*: **`reflection`** acts on your own session. `reflection(verb="compact", args={followup: "<next task>"})` queues a `/compact` behind your current turn plus a follow-up prompt, so the session compacts at end-of-turn and resumes with fresh context instead of going idle — the answer to a filling context mid-task, not a reason to stop. It works the same whether you are in a terminal or running as a background job, and it can only ever act on *you*: nothing in the surface takes a target, because your identity is observed from the `awm-mcp` proxy in front of you rather than passed as an argument. A caller it cannot identify (a plain shell) is refused rather than served with somebody else's session. Compact at a clean seam — a phase boundary, a finished chunk, crossing from planning into execution — and pass a `followup` naming the next task so the fresh context resumes pointed at the right work.

The **CLI and HTTP surfaces stay expanded** — `awm scope create`, `POST /invoke {name:"scope_create"}`, one command/route per verb — so only the MCP projection collapses; see the CLI note below.

## Skills

The end-of-session **debrief** is a native Claude Code skill (`~/.claude/skills/debrief/`): say "debrief" and the agent runs it — commit, journal (`scope_post kind=journal`), refresh. No MCP lookup needed. The debrief is for a coherent unit of finished work worth recording; in a fully autonomous run, that call is yours to make.

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
| `feat-fleet` | `feat/feat-fleet` | `svc-agents` |
| `dev` | `dev` | all promotable scopes (the `svc-*`, `web-*`, `rlm-*` set) |

This is the canonical, shared copy; each hub may mirror its own row into its `.awm/context.md` (gitignored, so local-only) for a hub agent to find it without walking up here.

For the day-to-day workflow of authoring/iterating on a service, page, or component — what files you write, the build + shadow flow — see `README.md` § *Authoring a service* / § *Authoring a page*; the internal architecture behind it is in the awm-internal `AGENTS.md` (auto-loaded inside any `projects/awm/*` scope).

## Dev protocol — parallel consumer/library scopes

Some projects **consume a shared-library project as a git submodule** and need to
work the *library* (its transforms) and the *consumer* in lockstep, across several
scopes at once. If every consumer scope pins the submodule to the **same** library
branch, parallel library edits collide on that one branch. The dev protocol gives
each consumer scope its **own** library branch + worktree, isolating library work
exactly the way code worktrees isolate consumer work — and it's a **reusable
template**, so a second consumer adopts it by filling slots.

**The N-slot template.** The consumer side uses role-generic scope names; the library
side names its parallel scopes **after the consumer**, so from inside the library
project you can tell which consumer effort a scope serves:

| Role | consumer scope | ↔ library scope |
|------|----------------|-----------------|
| lead / integrator | `dev` | `<consumer>` (hub) |
| parallel worker 1..3 | `dev1`, `dev2`, `dev3` | `<consumer>1`, `<consumer>2`, `<consumer>3` |

`dev`↔`<consumer>` are the two **hubs**; `devN`↔`<consumer>N` are the **peripherals** —
the same hub/peripheral shape as scatter/gather above, one pairing per project side.

**Submodule tracking (push-free, local).** Each consumer scope's `src/<lib>` submodule:

- carries an `awm` remote → the library project's local `.bare` (`origin` stays the
  GitHub url so `clone --recurse-submodules` still works);
- is checked out on its paired branch `feat/<consumer>N` with upstream set to
  `awm/feat/<consumer>N`;
- has `.gitmodules` `branch=` naming that same paired branch.

Sync is **fully local, never through GitHub**: the library worktrees and the consumer
submodule checkouts share one local `.bare` via the `awm` remote. Preferred workflow —
**edit transforms in the library worktree** (`projects/<lib>/<consumer>N`); commits
land straight in the shared bare; then in the consumer scope `git -C src/<lib> fetch
awm` and bump the gitlink. Editing inside the submodule instead? `git push awm
HEAD:feat/<consumer>N` targets the local bare — still no GitHub.

**Merging a worker up is a parallel merge.** Promoting `devN` is two gathers, one per
side: merge the library peripheral `feat/<consumer>N` into the library hub
`feat/<consumer>`, **and** merge the consumer peripheral into the consumer hub `dev`,
then bump the consumer hub's gitlink to the new library-hub tip. Drive each side with
`scope(verb="gather", …)` (hub + its peripherals) as usual.

Gotchas that bite this specifically: `git submodule update --remote` follows
`.gitmodules` `branch=` on the **default** remote (origin=GitHub), not `awm` — so sync
with explicit `fetch awm` / `push awm`, never `update --remote`. And `git worktree
move` **refuses on a worktree containing submodules** — move the dir by hand, then
`git worktree repair`, rename the `.bare/worktrees/<name>` admin dir to match, and fix
each submodule's `.git` gitdir pointer + `core.worktree`.

**Current instantiation — `fabfos` consuming `metasmith-libraries`:**

| consumer `fabfos` scope | branch | ↔ `metasmith-libraries` scope | library branch |
|---|---|---|---|
| `dev` (lead) | `dev` | `fabfos` (hub) | `feat/fabfos` |
| `dev1` | `feat/dev1` | `fabfos1` | `feat/fabfos1` |
| `dev2` | `feat/dev2` | `fabfos2` | `feat/fabfos2` |
| `dev3` | `feat/dev3` | `fabfos3` | `feat/fabfos3` |

## Git Model

Each project uses a **bare repo** at `projects/{project}/.bare/` with worktrees per scope.

- Branch naming: `feat/{scope}` (or flat keyword for legacy scopes).
- PRs created from feature branches into `main` / `release` as appropriate.
- See `.awm/skills/tools/git.md` for the worktree-bare flow in detail.

**Never commit in the workspace root checkout.** On a node that deploys rather than
authors awm, `<workspace>/` is a deploy *target*: it is fetched and `reset --hard` onto
upstream `release`, so a commit made there is silently discarded by the next deploy — and
a file left untracked there survives only until someone runs `git clean`. Nothing warns
you; the work is simply gone. All work belongs in a scope worktree under `projects/`,
pushed to a branch. Check with `git -C <workspace> reflog` before trusting a commit there:
a `reset: moving to …` entry is the tell.

## CLI Quick Reference

`awm <command> --help` for full options on any of these. The MCP tools above are usually more ergonomic from inside an agent — the CLI is for shell-level work.

**The CLI mirrors the full expanded surface.** Beyond the gateway-control commands in the table below, the CLI generates one `awm <domain> <verb>` command per registered feature-service tool — `awm scope create`, `awm agent list`, `awm project search`, etc. — from the **same live catalog** the MCP surface reads (the default `GET /tools`, the per-verb projection), so the two never drift and a newly-registered service's verbs appear with no extra wiring. Note the asymmetry: the **MCP** projection collapses to one generic `{verb,args}` tool per domain (`GET /tools?view=domains`), but the **CLI/HTTP** surfaces stay fully expanded — one `awm <domain> <verb>` command and one `POST /invoke {name:"<domain>_<verb>"}` per verb — so shell ergonomics are unchanged. `awm <domain> --help` lists a domain's verbs; `awm <domain> <verb> --help` shows that tool's exact parameters straight from its `inputSchema` (all `--flag` options). When the gateway is down the CLI lists from a cached snapshot; when it's up it's live-accurate every invocation.

| Command | Purpose |
|---|---|
| `awm gateway init` / `awm gateway status` / `awm gateway serve` / `awm gateway stop` / `awm gateway restart` | Core lifecycle |
| `awm project create <name>` | Create a project (optionally `--clone` / `--fork`) |
| `awm scope create <p> <s>` / `awm scope list` / `awm scope complete <p> <s>` | Scope worktree management |
| `awm scope heal [--project P] [--dry-run]` | Idempotent repair pass: enforce tier-3 = `.awm/` only, and bring `.awm/data` to the project's current data mode |
| `awm scope data-status <p> <s>` / `awm scope data-mount <p> <s> [--chunks ...]` | A scope's data view; which chunks materialise here |
| `awm scope data-gc --projects <p> ... [--dry-run false]` | Reclaim shared-cache space — names every project to keep |
| `awm dvc sync` / `awm dvc pull --scope <path>` / `awm dvc coverage` | Off-site: push the cache, restore one scope, audit what is uncovered |
| `awm session log <p> <s> --summary ... --decision ...` | Record a session entry |
| `awm gateway register / list / deregister` | Service Hub control plane (awm-internal — see AGENTS.md) |

## Agent Rules

1. **Raw data is immutable** — never modify files in `data/{project}/raw/`.
2. **Write outputs to `data/`, then `dvc add` and commit the pin** alongside the
   code that produced them. On an unconverted project `data/` is the shared
   directory and outputs are visible to siblings immediately. See § *Data* above.
3. **Don't edit `.awm/history.md`** — auto-generated. Use MCP tools.
4. **Run the `debrief` skill** when ending a session — commit, log the session, refresh.
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

WORKSPACE.md is the structural orientation every scope agent inherits at session start, for any project in the workspace: the workspace layout and per-scope `.awm/` paths, how the MCP tool surface is projected and discovered, the startup ritual, scope lifecycle and naming conventions, the git model, and the workspace-wide agent + Python-environment rules. awm-internal architecture goes in `AGENTS.md`; human install/usage goes in `README.md`.

**Nothing enumerable goes in here.** The test is whether the list changes without this file changing — a roster of projects, scopes, services, MCP domains, or verbs all fail it, and each one silently rots into a lie an agent then acts on. Write the *shape* and the discovery command instead; the runtime is the source of truth. The two standing exceptions are lists that are themselves the convention rather than a snapshot of state: the scope-prefix families and the hub/peripheral table, which are decisions this file makes, not facts it reports.

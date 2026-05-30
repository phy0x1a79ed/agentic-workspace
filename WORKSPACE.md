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

## Scope Naming Convention

New scopes use a prefix family to signal what kind of work they own. Names are flat (slashes are rejected — see `awm/services/_validation.py`), so the family is encoded as a hyphen-prefix.

| Prefix | Family | What it owns |
|--------|--------|-------------|
| `comp-*` | component | A single frontend component + its fixtures (one slug under `/dev/components/`). |
| `svc-*`  | service   | A backend service contract — endpoints, models, the Pydantic surface for an area. |
| `feat-*` | feature   | End-to-end integration that wires components, services, and external engines together (e.g. `feat-stt`, `feat-rooms`). |
| `infra-*`| infrastructure | Cross-cutting toolchain that other scopes consume — codegen, dev surfaces, test runners. |

Older scopes (`dev`, `sentry`, `vagrant-*`, `voice`, `web-ui`) predate this convention and keep their flat keyword names. The prefix family applies to scopes created from this point forward.

The dev surface that backs the `comp-*` family lives at `/dev/components/[name]` (see `infra-dev-components`).

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

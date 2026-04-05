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
      knowledge.md               # auto-generated: experiences + gotchas
      artifacts.md               # auto-generated: project artifact index
      data -> ../../../data/{project}/  # symlink to shared project data
      skills -> ../../../awm/skills/    # symlink to skill catalog
    [code files...]              # the actual repo content
```

Scopes access project data via `.awm/data/`. All scopes in the same project share the same data directory.

## Scope Lifecycle

1. **Create**: `scope_create` sets up a git worktree on `feat/{scope}` with `.awm/` metadata.
2. **Startup**: Agent reads `.awm/context.md`, runs `awm_refresh`, reads `knowledge.md` + `artifacts.md`.
3. **Work**: Code in the current directory. Data at `.awm/data/`. Skills at `.awm/skills/`.
4. **Debrief**: User says "debrief" — agent follows `skills_get path="awm/debrief.md"`.
5. **Complete**: `scope_complete` updates DB status, optionally merges branch.

## Skills System

Skills are dynamic protocols that improve with use:

- **AWM skills** (`skills/awm/`): workspace procedures that drive the MCP tool surface (create-project, create-scope, debrief, skill-update)
- **Tool guides** (`skills/tools/`): external-tool references (git, mamba, dependencies, mcp, metasmith, plotly)
- Skills have frontmatter with `tags`, `requires`, and `scope` for search and hierarchy
- `skills_search` combines keyword + semantic search (sentence-transformers embeddings)
- **Experiences** are execution traces attached to skills — log what happened, deviations, and improvement suggestions
- A dedicated `awm/skill-improvement` scope periodically reads experiences and revises skills

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
3. **Don't edit `.awm/knowledge.md` or `.awm/artifacts.md`** — these are auto-generated. Use MCP tools.
4. **Follow the debrief skill** when ending a session — log experiences, register artifacts, reflect.
5. **Search skills first** — use `skills_search` before starting unfamiliar workflows.

# AWM Workspace

*Orientation for any agent working in a scope worktree of this AWM workspace. Injected into every scope agent's context: Claude Code Reads it at session start per `~/.claude/CLAUDE.md`; OpenCode auto-injects it via the per-scope `mcp-opencode.json` `instructions` array.*

Context is assembled general → specific: this file, then the cwd-local `AGENTS.md`, then `.awm/context.md`.

Operating the workspace — creating scopes, naming them, moving work between them, managing data and backups, the CLI surface — is in `AGENTS.md` beside this file. Read it when you need a procedure.

## Per-Scope Layout

Agents land directly in the git worktree. All AWM metadata lives in a `.awm/` dotdir inside:

```
projects/{project}/
  .bare/                         # bare git repo
  {scope}/                       # git worktree — agent CWD; {scope} may nest
    .awm/                        # AWM metadata (gitignored)
      context.md                 # scope instructions (auto-loaded)
      history.md                 # auto-generated: open/resolved session history
      data -> ../data            # compat symlink; the real data/ is repo content
      skills -> <workspace>/awm/skills/  # absolute symlink to skill catalog
    [code files...]              # the actual repo content, including data/
```

Both symlinks are depth-independent, which is what lets `{scope}` nest: `data` is relative to its own worktree and `skills` is absolute.

## Startup Ritual

Every scope agent runs this on session start, and may re-run it any time to refresh:

1. `scope(verb="refresh", args={project:<p>, scope:<s>})` — re-renders `.awm/history.md` from the DB.
2. Read `.awm/history.md` — open + resolved session log for this scope and its siblings.
3. `scope(verb="fetch", args={scope:<s>, kind:"message"})` — anything addressed to you that is waiting.

`.awm/history.md` is auto-generated. Never edit it by hand — use the `scope` domain's verbs.

## MCP Tools

The MCP server (`awm-mcp`) is registered at `<workspace>/.mcp.json` and auto-discovered by MCP clients. The surface is **projected live** from whatever feature services are registered and **collapsed by domain**: your client sees one generic tool per domain — `scope`, `project`, `agent`, `services`, … — each called with `{ "verb": "<name>", "args": { … } }`.

**The catalog is self-describing — discover it, don't memorize it.** The set grows every time a service registers, so no list written here would stay true. Three moves:

1. **Which domains exist** — the domain tools your client exposes *are* the catalog. In a client that defers schemas a domain shows as a bare name until loaded, so surface one with `ToolSearch`, or list the running services with `services(verb="list")`.
2. **What a domain can do** — call it with `verb="describe"` (optionally `args={"verb":"<name>"}`) for its verbs and parameter schemas. `describe` is reserved on every domain.
3. **Which node it runs on** — the envelope's third key, `peer`. Omitting it uses the domain's default provider; `providersOf(tool="<domain>")` reports the valid values. A misdirected `peer` is refused naming the options, never quietly run locally. See `FEDERATION.md` § *Cross-peer calls*.

So the reflex when a task *looks* like it needs a human — send a message, approve a login, capture audio, bounce a VPN — is to `describe` a plausible domain or `ToolSearch` first. Server-side, a placed agent's mode restricts which verbs it may call regardless of harness, so a disallowed verb is rejected rather than silently honored.

Three domains change how you work rather than what you can do:

- **`graphify`** — an AST knowledge graph of the awm source tree. Reach for it before dispatching an Explore agent on any "where is X / what calls or imports Y / impact of changing Z" question about awm. It indexes the deployed tree, not your uncommitted worktree, so use Explore for code you just wrote and for other projects.
- **`precedence`** — an archive of past user-adjustment decisions. Search it before re-asking the user a preference-shaped question, and contribute back whenever the user makes or overrides such a decision.
- **`reflection`** — acts on your own session. `reflection(verb="compact", args={followup:"<next task>"})` queues a compaction behind the current turn plus a follow-up prompt, so a filling context is a seam to cross rather than a reason to stop. It takes no target: your identity is observed from the proxy in front of you, so a caller it cannot identify is refused rather than served with somebody else's session.

The **CLI and HTTP surfaces stay expanded** — `awm <domain> <verb>`, `POST /invoke {name:"<domain>_<verb>"}` — so only the MCP projection collapses.

## Git Model

Each project uses a **bare repo** at `projects/{project}/.bare/` with worktrees per scope.

- Branch naming: `feat/{scope}` by default, but a legacy or nested scope carries its own name. The DB row records which — nothing recomputes it from the scope name, so ask `scope(verb="search")` rather than guessing.
- PRs go from feature branches into `main` / `release` as appropriate.

**Never commit in the workspace root checkout.** On a node that deploys rather than authors awm, `<workspace>/` is a deploy *target*: it is fetched and `reset --hard` onto upstream `release`, so a commit made there is silently discarded by the next deploy, and an untracked file survives only until someone runs `git clean`. Nothing warns you. All work belongs in a scope worktree under `projects/`, pushed to a branch. `git -C <workspace> reflog` shows the tell: a `reset: moving to …` entry.

## Agent Rules

1. **Raw data is immutable** — never modify files under a project's `raw/`.
2. **Write outputs to `data/`, then commit the pin** alongside the code that produced them. See `AGENTS.md` § *Data*.
3. **Don't edit `.awm/history.md`** — it is generated.
4. **Run the `debrief` skill** when ending a session.
5. **Check `.awm/skills/` for a procedure** before improvising an unfamiliar workflow.

## Python Environment Rules

System Python is externally managed (PEP 668) — `pip install` is blocked.

**Do NOT use** `python`, `python3`, `pip`, `pip3` directly, or `conda activate` / `mamba activate` (they need an interactive shell init).

**Always use:**

```bash
mamba run -n <project-env> python script.py
mamba run -n <project-env> pip install <package>
```

For AWM itself: `mamba run -n awm <cmd>`.

## What goes in this file

WORKSPACE.md is the orientation a scope agent needs on any turn in any project: where it is, how to discover the tool surface, what to run at startup, and the rules whose violation loses work. It is injected into every scope agent's context, so every line here costs every session.

Operating the workspace — creating scopes, naming them, moving work between them, managing data and backups — goes in the workspace `AGENTS.md`. What a project is for goes in that project's own files.

Nothing enumerable goes here. The test is whether the list changes without this file changing.

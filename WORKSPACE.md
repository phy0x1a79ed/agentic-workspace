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
      skills -> <workspace>/skills/      # absolute symlink to skill catalog (SKILLS_DIR)
    [code files...]              # the actual repo content, including data/
```

Both symlinks are depth-independent, which is what lets `{scope}` nest: `data` is relative to its own worktree and `skills` is absolute.

### Retiring a project

**Archive it; never delete it.** A retired project is renamed
`<name>-ARCHIVED-<YYYYMMDD>`, gets an `ARCHIVED.md` at its top naming where the content
went and where its bundle is, and is then `chmod -R a-w`. Retire each of its scopes
(`scope complete`, no cleanup) so the worktrees stay on disk as read-only record.

Three things about that are not obvious:

- **Bundle first, and verify.** `git bundle create <f> --all` then `git bundle verify`,
  and check every live ref tip is an object in the bundle. A local branch that exists on
  no remote — the usual reason a project is being retired rather than abandoned — has no
  other backup, and the bundle belongs somewhere DVC-pinned so the append-only archive
  job carries it off-site.
- **The listing shows it twice, and that is not a bug.** `project search` unions the
  scope database with the on-disk `<name>/.bare` directories, so an archived project
  appears under its database name with zero active scopes *and* under its new directory
  name with none at all. Neither is active; `active_only` drops both.
- **The shared cache does not know it is archived.** `data_gc` keeps only what the
  projects you *name* pin, so once a project is retired, objects reachable only from its
  pins are collectable by any gc that omits it. Migrate the chunks that matter before
  archiving, and treat the rest as gone.

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

## Consuming another project's code

**Co-locate it in one repository; do not submodule it.** A consumer and the library
it moves in lockstep with belong in one project, as directories — which is what
`projects/metasmith/` is: the engine, the standard transform library, fabfos and
ASPIRE, one history, no pins. A scope layer names the product (see *Nested names*).

The workspace ran the other protocol for a year: each consumer scope carried its own
library branch and worktree, and promoting one worker meant merging both sides and
bumping a gitlink. It worked. What retired it is that every one of those steps was a
place for the pin and the branch to disagree, and a stale gitlink is silent — nothing
downstream reports that a consumer is building against a library commit nobody has
worked on for a month. Co-location makes the whole class unrepresentable.

Nothing here consumes a submodule now. If one ever must, two facts cost a day each
and are not discoverable from a failure message: `git submodule update --remote`
follows `.gitmodules` `branch=` on the **default** remote, so a local-only sync has to
be an explicit `fetch`/`push` against the sibling bare; and `git worktree move`
**refuses on a worktree containing submodules**, so the move is by hand, followed by
`git worktree repair`, renaming the `.bare/worktrees/<name>` admin dir, and fixing
each submodule's `.git` gitdir pointer and `core.worktree`.

## Git Model

Each project uses a **bare repo** at `projects/{project}/.bare/` with worktrees per scope.

- Branch naming: `feat/{scope}` by default, but a legacy or nested scope carries its own name. The DB row records which — nothing recomputes it from the scope name, so ask `scope(verb="search")` rather than guessing.
- PRs created from feature branches into `release`. There is no `main` — it was
  retired 2026-08-15 as a strict ancestor of `release` that had drifted 875
  commits behind while still being GitHub's default branch, which is exactly how
  a stale branch gets mistaken for a baseline.

**A peer's bare has most of its branches checked out, so pushes to it are
refused.** Every scope worktree on a node holds its branch, and `release` is the
node's live workspace — so `git push <peer> <branch>` fails with *branch is
currently checked out* for nearly every branch worth pushing, which reads like a
permissions or connectivity fault and is not one. Push to a temp ref, then
fast-forward it into place *inside the target worktree* (`git -C <wt> merge
--ff-only <tempref>`), and delete the temp ref. Prefer `merge --ff-only` over
`reset --hard`: it refuses rather than discards when that worktree has
uncommitted edits, which it often does. Stash first if you need it to pass.

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

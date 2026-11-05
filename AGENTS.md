# AWM Workspace

## Purpose & Contents

This file orients any agent working in this workspace. Claude Code loads it into every session under the workspace root. Every line costs every session. It holds only what an agent needs on any turn in any project: where it is, what to run at startup, how to find the tool surface, and the rules whose violation loses work.

Read the companion files by path when a task needs them:

- `PROTOCOLS.md` — operating the workspace: scope lifecycle and naming, hub integration, data and backups, retiring a project, the CLI.
- `ARCHITECTURE.md` — awm internals, for agents that change awm itself.
- `FEDERATION.md` — anything that crosses nodes.
- `README.md` — human install and usage.

Nothing enumerable goes here. If a list changes without this file changing, the list belongs somewhere else.

## Where you are

Each project is a bare repo at `projects/<project>/.bare/` with one git worktree per scope. You start in the worktree:

```
projects/<project>/
  .bare/                  # bare git repo
  <scope>/                # git worktree and your cwd. <scope> may nest
    data/                 # versioned data, see § Data
```

Find projects and scopes live. A list in a doc goes stale within days.

- `scope(verb="search")` with no `project` lists every project.
- `scope(verb="search", args={project:<p>})` lists a project's scopes, branches and worktrees.

## Startup ritual

Run this at session start. Run it again any time to refresh.

1. Run `journal(verb="inbox", args={project:<p>, scope:<s>})` for the messages your scope has not read, one line each. Read one in full with `journal(verb="fetch", args={post_id:<id>})`. Run `journal(verb="ack", args={project:<p>, scope:<s>, through:<ack_through>})` once you have read them, so the next session starts after them.

`journal(verb="render", args={project:<p>, scope:<s>})` returns the scope's latest journals, then its project's, when you need its history.

## MCP tools

The `awm` MCP server (`awm mcp`) is registered in `<workspace>/.mcp.json`. Its surface comes live from the running feature services: one top-level tool per domain, such as `scope`, `journal`, `search` and `awm`. Call each with `{ "verb": "<name>", "args": { … } }`.

Discover the catalog. Do not memorize it.

1. Run `awm(verb="describe")`. One call returns every tool with its verbs, each verb with a one-line summary and its required arguments. A client that defers schemas shows a tool as a bare name. Load it with `ToolSearch` first.
2. Run `<tool>(verb="describe")` for all of that tool's verbs, their params and which params are required. A param lists its `type` only when it is not a string. Add `args={"verbs":["<name>"]}` for some of them. An unknown argument to a verb returns an error that carries its schema.
3. Add the optional `peer` key to run a verb on a node across an edge. Its enum lists the valid nodes and its description names the default. A tool that one node serves routes there with no `peer`. A wrong `peer` is refused, never run locally.

Check for a tool before you hand a task to the user. Sending a message, approving a login, capturing audio and restarting a VPN each look like human jobs, and each has a domain. A restricted session's mode limits which verbs it may call, and the server rejects the rest.

Four tools change how you work:

- `search` — one ranked search over journals, precedent posts, papers, transcripts and the awm tools, on this node and across its edges. Query it before you ask what the swarm already knows. Read a whole hit with `get`.
- `journal` precedents — past user decisions on preferences, as journal posts tagged `precedent`. Search them before you ask the user a preference question. Post one when the user makes or overrides such a decision.
- `reflection` — acts on your own session. `reflection(verb="compact", args={followup:"<next task>"})` queues a compaction after this turn, then sends the follow-up prompt. A full context is a seam to cross, not a reason to stop.
- `operations` — keeps your task on Tony's command center. When you need Tony in a background session, call `operations(verb="request_human", args={kind:"blocked", reason:<why>})` instead of `AskUserQuestion`, because nobody watches that terminal. Call it with `kind:"review"` when the work is ready to look at.

The CLI (`awm <tool> <verb>`) and HTTP (`POST /invoke`) surfaces keep one command per verb, hidden verbs included. Only the MCP surface collapses.

## Git

- A scope's branch is `feat/<scope>` by default. A legacy or nested scope has its own branch name. Ask `scope(verb="search")` instead of guessing.
- Land your own work with `routine(verb="deploy")` from your scope. It promotes `feat → dev → release` on mizar and refreshes each node, as `FEDERATION.md` § Deploy describes. There is no `main`.
- Do not open pull requests. Open one only when Tony asks for it.
- **WARNING** Never commit in the workspace root checkout. On a node that deploys awm, each deploy resets `<workspace>/` onto upstream `release` and discards local commits without a warning. Work in a scope worktree under `projects/` and push the branch. A `reset: moving to …` entry in `git -C <workspace> reflog` shows a deploy ran.
- A push to mizar fails with *branch is currently checked out* when a worktree there holds the branch. This is not a permissions fault. See `PROTOCOLS.md` § *Pushing to mizar*.
- Do not submodule another project's code. Put a consumer and the library it moves with in one repository, as directories. A stale gitlink fails silently. `projects/metasmith/` is the model.

## Data

A commit versions the code and the data it was built against together. A DVC-backed project keeps files at `data/<chunk>` and a tracked pin at `data/<chunk>.dvc`. The bytes live once, in a cache that every scope on the machine shares.

- To save data, run `dvc add data/<chunk>`. Commit the changed pin with your code.
- To take a sibling's data, merge their branch. A post-merge hook checks the files out.
- Write outputs to `data/`. Never modify files under a project's `raw/`.
- **WARNING** Materialised files are read-only hardlinks into the shared cache. An in-place edit corrupts that object for every scope and every commit that pins it. Write a new file, or run `dvc unprotect <path>` first.
- **WARNING** Never run a bare `dvc gc`. It collects against one worktree's view of a cache the whole workspace shares. Use `dvc(verb="gc")`, which makes you name what to keep.

A project without a tracked `.dvc/config` has no data wiring. `PROTOCOLS.md` § *Data* covers the rest.

## Python

System Python is externally managed, so `pip install` fails. Do not run `python`, `pip`, `conda activate` or `mamba activate` directly. Run every command through `mamba run`:

```bash
mamba run -n <project-env> python script.py
mamba run -n <project-env> pip install <package>
```

awm itself uses the `awm` env.

## Ending a session

Run the `debrief` skill. It corrects docs the work made wrong, commits code and data pins, and journals the session.

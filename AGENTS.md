# AWM Workspace

## Purpose & Contents

This file orients any agent working in this workspace. Claude Code loads it into every session under the workspace root, and OpenCode loads it through each scope's `mcp-opencode.json`. Every line costs every session. It holds only what an agent needs on any turn in any project: where it is, what to run at startup, how to find the tool surface, and the rules whose violation loses work.

Read the companion files by path when a task needs them:

- `PROTOCOLS.md` — operating the workspace: scope lifecycle and naming, hub integration, data and backups, retiring a project, the CLI.
- `ARCHITECTURE.md` — awm internals, for agents that change awm itself.
- `FEDERATION.md` — anything that crosses nodes.
- `README.md` — human install and usage.

Nothing enumerable goes here. If a list changes without this file changing, the list belongs somewhere else.

## Where you are

Each project is a bare repo at `projects/<project>/.bare/` with one git worktree per scope. You start in the worktree. AWM metadata lives in its `.awm/` dotdir:

```
projects/<project>/
  .bare/                  # bare git repo
  <scope>/                # git worktree and your cwd. <scope> may nest
    .awm/                 # gitignored
      context.md          # this scope's brief
      history.md          # generated session log
      data -> ../data     # compat symlink. The real data/ is repo content
    data/                 # versioned data, see § Data
```

Find projects and scopes live. A list in a doc goes stale within days.

- `scope(verb="search")` with no `project` lists every project.
- `scope(verb="search", args={project:<p>})` lists a project's scopes, branches and worktrees.

## Startup ritual

Run this at session start. Run it again any time to refresh.

1. Run `journal(verb="render", args={project:<p>, scope:<s>})`. It renders `.awm/history.md` from the journal.
2. Read `.awm/context.md`. It states why this scope exists.
3. Read `.awm/history.md`.
4. Run `journal(verb="fetch", args={scope:<s>, kind:"message"})` for messages addressed to you.
5. Run `journal(verb="goal_read", args={project:<p>, scope:<s>})` for the goal in force. Read it again before you propose a deliverable. The `goal-alignment` skill sets or revises it.

Never edit `.awm/history.md` by hand. Use the `journal` verbs.

## MCP tools

The `awm-mcp` server is registered in `<workspace>/.mcp.json`. Its surface comes live from the running feature services: sixteen top-level tools, such as `scope`, `journal`, `search` and `awm`. Call each with `{ "verb": "<name>", "args": { … } }`.

Discover the catalog. Do not memorize it.

1. Run `awm(verb="describe")`. One call returns the most used verbs, each with a summary and its required arguments, and one line for every tool. A client that defers schemas shows a tool as a bare name. Load it with `ToolSearch` first.
2. Run `<tool>(verb="describe")` for all of that tool's verbs and parameter schemas. Add `args={"verbs":["<name>"]}` for some of them. An unknown argument to a verb returns an error that carries its schema.
3. Add the optional `peer` key to run a verb on another node. Its enum lists the valid nodes and its description names the default. A tool that one node serves routes there with no `peer`. A wrong `peer` is refused, never run locally.

Check for a tool before you hand a task to the user. Sending a message, approving a login, capturing audio and restarting a VPN each look like human jobs, and each has a domain. A restricted session's mode limits which verbs it may call, and the server rejects the rest.

Five tools change how you work:

- `search` — one ranked search over journals, precedence decisions, papers and other archives, on this node and its peers. Query it before you ask what the swarm already knows. Read a whole hit with `get`.
- `graphify` — an AST graph of the awm source tree. Query it before you start an Explore agent on "where is X, what calls Y, what does changing Z affect" in awm. It indexes the deployed tree. Use Explore for uncommitted code and for other projects.
- `precedence` — an archive of past user decisions on preferences. Search it before you ask the user a preference question. Add to it when the user makes or overrides such a decision.
- `reflection` — acts on your own session. `reflection(verb="compact", args={followup:"<next task>"})` queues a compaction after this turn, then sends the follow-up prompt. A full context is a seam to cross, not a reason to stop.
- `operations` — keeps your task on Tony's Swarm board. When you need Tony in a background session, call `operations(verb="request_human", args={kind:"blocked", reason:<why>})` instead of `AskUserQuestion`, because nobody watches that terminal. Call it with `kind:"review"` when the work is ready to look at.

The CLI (`awm <tool> <verb>`) and HTTP (`POST /invoke`) surfaces keep one command per verb, hidden verbs included. Only the MCP surface collapses.

## Git

- A scope's branch is `feat/<scope>` by default. A legacy or nested scope has its own branch name. Ask `scope(verb="search")` instead of guessing.
- Open PRs from feature branches into `release`. There is no `main`.
- **WARNING** Never commit in the workspace root checkout. On a node that deploys awm, each deploy resets `<workspace>/` onto upstream `release` and discards local commits without a warning. Work in a scope worktree under `projects/` and push the branch. A `reset: moving to …` entry in `git -C <workspace> reflog` shows a deploy ran.
- A push to a peer usually fails with *branch is currently checked out*. This is not a permissions fault. See `PROTOCOLS.md` § *Pushing to a peer*.
- Do not submodule another project's code. Put a consumer and the library it moves with in one repository, as directories. A stale gitlink fails silently. `projects/metasmith/` is the model.

## Data

A commit versions the code and the data it was built against together. A DVC-backed project keeps files at `data/<chunk>` and a tracked pin at `data/<chunk>.dvc`. The bytes live once, in a cache that every scope on the machine shares.

- To save data, run `dvc add data/<chunk>`. Commit the changed pin with your code.
- To take a sibling's data, merge their branch. A post-merge hook checks the files out.
- Write outputs to `data/`. Never modify files under a project's `raw/`.
- **WARNING** Materialised files are read-only hardlinks into the shared cache. An in-place edit corrupts that object for every scope and every commit that pins it. Write a new file, or run `dvc unprotect <path>` first.
- **WARNING** Never run a bare `dvc gc`. It collects against one worktree's view of a cache the whole workspace shares. Use `dvc(verb="gc")`, which makes you name what to keep.

A project without a tracked `.dvc/config` keeps a shared, unversioned `.awm/data` symlink to `data/<project>/`. `PROTOCOLS.md` § *Data* covers the rest.

## Python

System Python is externally managed, so `pip install` fails. Do not run `python`, `pip`, `conda activate` or `mamba activate` directly. Run every command through `mamba run`:

```bash
mamba run -n <project-env> python script.py
mamba run -n <project-env> pip install <package>
```

awm itself uses the `awm` env.

## Ending a session

Run the `debrief` skill. It corrects docs the work made wrong, commits code and data pins, and journals the session.

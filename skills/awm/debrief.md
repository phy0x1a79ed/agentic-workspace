---
name: debrief
tags: [session, completion, reflection, debrief]
requires: []
description: End-of-session debrief — commit the session's work, journal it (scope_post kind=journal), reconcile artifacts, refresh
---

# Session Debrief

Run this when told to debrief. Its job is to leave a **clean, recoverable handoff**: the
session's work persisted into durable stores (git, the journal, the artifact registry), the
shared docs and indexes kept true, and the next session able to resume without archaeology.
It is a checkpoint, not a completion gate — done-ness is recorded in the journal's `outcome`,
not by what you leave uncommitted.

A scope **is** its channel: the journal is a `scope_post kind=journal` keyed by
`(project, scope)`. There is no `session_log` tool, and entries aren't "resolved" — open
threads carry forward by being referenced in the next one.

> **Call convention.** The `<domain>_<verb>` names below (`scope_post`, `artifact_sync`, `scope_refresh`, …) are **operation** names. On the collapsed MCP surface, call them as the domain tool's verb — `scope(verb="post", args={kind:"journal", …})`, `artifact(verb="sync")`, etc. (`scope(verb="describe")` lists a domain's verbs). On the CLI/HTTP surfaces they stay expanded (`awm scope post …`, `POST /invoke {name:"scope_post"}`).


## 1. Commit the session's work

Get your changes out of the working tree and into git — this is what makes the work *landed*
rather than dirty state someone else has to untangle.

- **Bring the docs up to date, if behavior changed.** Make `AGENTS.md` / project docs (and
  `.awm/context.md` for this scope's framing) describe the project as it is *now* — add
  what's newly true and **delete what's no longer true**. Docs are forward-looking reference:
  write what a future session needs to know, never a changelog or a record of what this
  session did (that's the journal's job, step 2). Skip when nothing durable changed — don't
  re-touch to "refresh." Never hand-edit `.awm/history.md` / `.awm/artifacts.md`
  (auto-generated; rebuilt in step 4). `.awm/` is gitignored, so context.md is saved, not
  committed.
- **Then commit.** Stage your files by path (`git add <paths>`) and commit; don't blanket
  `git add -A` a worktree you may share. If the tree holds pre-existing changes from another
  scope or stream, commit only your paths and leave the rest. Don't withhold the commit
  because the work is unfinished — commit it and mark the journal `outcome: partial`. Only
  skip if the user said not to, or the changes aren't yours to land; if you skip, note why in
  the journal.

## 2. Journal the session

    scope_post project={project} scope={scope} kind=journal \
      author="agent:{project}/{scope}" \
      body="<narrative>" \
      meta='{"title":"...","outcome":"success","skill_path":"awm/debrief.md"}'

- **`body`** — the substance, and the only place detail survives. Lead with the commit
  SHA(s), then **Decisions**, **Issues**, **Next steps** as bullets. For unfinished work, say
  what's done, what's left, where to resume. `history.md` renders only title + outcome, so a
  future session reads the body back via `scope_fetch` — put it here.
- **`meta`** — `title` (≤ ~80 chars), `outcome` (`success` | `partial` | `failure` |
  `abandoned` — where done-ness lives), `skill_path` (keep `awm/debrief.md` so journals group
  for skill-improvement), optional `deviations` / `suggestions`.

## 3. Carry threads forward

You read `.awm/history.md` at session start; act on any open threads it surfaced by
referencing them here. Only `scope_fetch project={project} scope={scope} kind=journal
order=desc` if you need a prior body you don't already have.

## 4. Reconcile artifacts and refresh

- **If outputs changed:** `artifact_search project={project} scope={scope}` to see what
  exists, `artifact_delete artifact_id=<id>` for superseded ones, `artifact_register
  project={project} scope={scope} name=... artifact_type=... path=... description=...` for
  new/changed (upserts on `path`; path relative to workspace root; types:
  figure/dataset/report/model/script/other). Skip if nothing changed.
- **Always:** `artifact_sync` (lazy no-op when clean) then `scope_refresh project={project}
  scope={scope}` to rebuild the indexes for the next session.

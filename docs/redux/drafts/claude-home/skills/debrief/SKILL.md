---
name: debrief
description: End-of-session debrief for an AWM scope — run when the user says "debrief" (or to checkpoint a session). Updates docs if behavior changed, commits the session's code and data pins, records Tony's decisions as precedent posts, and journals the session with tags=[journal] and its citations in reply_to, leaving a clean recoverable handoff.
---

# Session Debrief

Run this when told to debrief. Its job is to leave a **clean, recoverable handoff**: docs kept
true, the session's work landed in git, Tony's decisions recorded, and the session journaled so
the next one resumes without archaeology. It is a checkpoint, not a completion gate — done-ness
lives in the journal's `outcome`, not in what you leave uncommitted.

## 1. Update docs (only if behavior changed)

If this session changed how the project behaves, make `AGENTS.md` and the project docs describe
the project as it is *now*. Load the `write-docs` skill and follow it.

When the scope's purpose changed, revise its brief with `journal brief_edit` or `brief_set`. A
brief holds two sentences at most.

Prune as you edit. A session that only added to a doc did it wrong.

Skip this step when nothing durable changed. Do not re-touch a doc to refresh it. A change that
mattered only to this session belongs in the journal below.

## 2. Commit the code and the data together

Stage by path: `git add <paths>`. Do not `git add -A` a worktree you may share.

A DVC-backed scope is one whose worktree tracks a `.dvc/config` — test for that file. There,
data is versioned by the commit that versions the code, so a code-only commit leaves the
session's outputs unversioned:

1. Run `mamba run -n dvc dvc add data/<chunk>` for each chunk this session wrote. `dvc` is not
   on `PATH`.
2. Stage the changed `data/<chunk>.dvc` pins beside your code.
3. Commit the pins and the code together. One commit records the code and the exact data it
   was built against.

**CAUTION** A materialised data file is a read-only hardlink into the cache every scope shares.
Editing one in place corrupts that object for every commit that pins it. Record it in the
journal if you did.

Commit even when the work is unfinished, and mark the journal `outcome: incomplete`. Skip the
commit only when the user said not to, or the changes are not yours to land. Say why in the
journal.

A branch with no upstream is normal here and needs no push. The nightly mirror carries each
project's `.bare` repo off-machine, and the nightly append-only sync carries the DVC cache.
`dvc(verb="coverage")` still calls every such branch `at_risk` because it reads GitHub alone.
Do not raise it.

## 3. Record Tony's decisions as precedents

Each decision Tony made or overrode in this session that holds beyond it becomes one precedent
post, so a later session can act on it without asking him again. A choice that mattered only to
this task stays in the journal body.

    journal(verb="post", args={project:"", scope:"workspace", author:"agent:{project}/{scope}",
      tags:["precedent", "<topic tag>"], body:"<context, question, decision>"})

- Search the precedents first (`search` with `tags: "precedent"`). When an older decision
  covers the same question, reply to it with `reply_to:[<old id>]`, then mark the old one with
  `journal retag {post_id:<old id>, add:["superseded"]}`.
- The body holds the context, the question and the decision, with Tony's own words quoted.
- Topic tags come from the vocabulary (`journal tag_list`). An unknown tag is refused.

## 4. Journal

    journal(verb="post", args={project, scope, author:"agent:{project}/{scope}",
      tags:["journal"], reply_to:[<cited journal ids>],
      meta:{title:"...", outcome:"completed", skill_path:"awm/debrief.md"},
      body:"<narrative>"})

- **`body`** — the substance, and the only place detail survives. Lead with the commit SHA(s),
  then **Decisions**, **Issues**, **Next steps** as bullets. Carry the prior journal's open
  threads forward. Pass `body` last.
- **`reply_to`** — the journals this session built on: its citations. Run `journal reads` for
  the candidates, the journals this session fetched. Cite only the ones that shaped the work,
  by id, with no reason. A citation points only at a journal. When a source session left no
  journal, write one for it first (`meta.session` names that session), then cite it.
- **`meta`** — `title` (≤ ~80 chars), `outcome` (`completed` | `incomplete` | `aborted`, where
  `aborted` means cancelled or failed), `skill_path` (a grouping key, not a file path — keep
  `awm/debrief.md` so journals group under this skill), optional `deviations` / `suggestions`.

## 5. Hand the task back

Your task's card is on the command center. `reflection whoami` names its task id.

- Outcome `completed`: call `operations(verb="request_human", args={kind:"review", summary})`.
  Say what was done and name the journal id, in a few sentences. The card moves to Needs you.
  Call it last.
- Outcome `incomplete` because the work needs Tony: call
  `operations(verb="request_human", args={kind:"blocked", reason})` instead.
- Any other outcome: do nothing. The card stays in Working.

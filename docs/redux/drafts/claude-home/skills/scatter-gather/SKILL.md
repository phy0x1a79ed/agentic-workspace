---
name: scatter-gather
description: Fan-in / fan-out git integration across AWM scopes around a hub branch — run when asked to "gather" feature branches into a hub (dev / feat-dag / feat-gamebot) or "scatter" a hub back out to its peripheral feature branches. Drives the scopes service's gather/scatter verbs, reports per-branch outcomes, and points you at any conflicts to resolve by hand.
---

# Scatter / Gather

A recurring AWM integration chore: a **hub** scope (`dev`, `feat-dag`, `feat-gamebot`)
collects work from a set of **peripheral** feature scopes. Two directions:

- **gather** (fan-in) — merge each peripheral's `feat/<p>` branch *into* the hub branch.
  Do this before integrating/deploying a hub, to pull everyone's work together.
- **scatter** (fan-out) — merge the hub branch *back out* into each peripheral's
  `feat/<p>` branch. Do this after the hub advanced, so the peripherals stay current.

Both run real `git merge` across the worktrees, **local-only — they never push**. Both are
**stateless**: you pass the peripheral list explicitly. The verbs accept `strategy="merge"`
only (rebasing many branches around a shared hub is not what this workflow does).

**Calling convention (MCP domain collapse).** The gateway's MCP surface is one generic tool
per domain taking `{verb, args}`. Call `scope(verb="gather", args={…})` /
`scope(verb="scatter", args={…})`; `scope(verb="describe")` lists the verbs and their
schemas. On the CLI/HTTP the same verbs spell as `scope_gather` / `scope_scatter`
(`awm scope gather …`). If the `scope` domain reports "Unknown tool", the scopes service
isn't `ready` — say so rather than falling back to hand-merging silently.

## 1. Find the hub and its peripherals

Membership is a **documented convention, not stored state**. Read it from the committed
`PROTOCOLS.md` at the workspace root (§ *Hubs and peripherals*), the one copy of the
hub→peripheral map for `dev`, `feat-dag`, `feat-gamebot`.

If the user named the peripherals, use those. Otherwise take the documented list and
**confirm it with the user before a gather** (a fan-in is the side that mutates the hub).
The list is just the scope names (e.g. `["svc-events", "web-stt"]`) — the verb resolves each
to its `feat/<p>` branch (and the hub to its branch, which may be a legacy flat name like
`dev`) from the scopes DB.

## 2. Run the verb

**Gather (fan-in, before a hub integration/deploy):**

    scope(verb="gather", args={"project": "awm", "hub": "feat-dag",
                               "peripherals": ["svc-events", "web-stt", "web-tts"]})

CLI form: `awm scope gather --project awm --hub feat-dag --peripherals svc-events --peripherals web-stt`

Preconditions: the **hub worktree must be clean and checked out on the hub branch** — the
verb refuses to start otherwise (the whole fan-in needs a clean hub). It merges each
peripheral into the hub, in the hub worktree.

**Scatter (fan-out, after the hub advanced):**

    scope(verb="scatter", args={"project": "awm", "hub": "feat-dag",
                                "peripherals": ["svc-events", "web-stt", "web-tts"]})

Each merge runs in that peripheral's own worktree. A peripheral that is dirty or not on its
feat branch is **skipped** (one dirty sibling never aborts the batch).

## 3. Read the report

The verb returns `{project, hub, hub_branch, direction, results, summary}`. Each entry in
`results` is `{scope, branch, result, detail}` with `result` one of:

- `merged` — the merge landed.
- `up_to_date` — already current; nothing to do.
- `skipped` — branch/worktree missing, or (scatter) the peripheral was dirty / off-branch.
  See `detail` for which.
- `conflict` — the merge hit a conflict and was **rolled back** (`git merge --abort`), so
  the worktree is left clean. The batch continued to the next peripheral.
- `error` — an unexpected git failure; see `detail`.

`summary` counts results by outcome. Report this back to the user plainly: what merged, what
was already current, what was skipped (and why), what conflicted.

## 4. Resolve conflicts by hand

A `conflict` is **not** left half-merged — it was aborted, so the worktree is clean. To land
it, do that one merge manually in its worktree, resolve, commit, then re-run the verb for
just the still-pending scopes:

    cd projects/<project>/<hub-or-peripheral-worktree>
    git merge <other-branch>      # the same merge the verb attempted
    # resolve conflicts, then:
    git add <files> && git commit
    # then re-run gather/scatter for the remaining peripherals only

For **gather**, the conflict is in the hub worktree merging `feat/<p>`. For **scatter**, it's
in the peripheral's worktree merging the hub branch.

## Notes

- These verbs are **local-only**. Promoting a hub *onward* — `dev` → `release`, the prod
  service bounce, GitHub push — is a separate, deliberate step (see the deploy notes in
  memory / AGENTS.md); this skill never pushes.
- This skill lives in `~/.claude/skills/` — it is installed once on the machine and does not
  travel with a branch merge.

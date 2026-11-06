# resume-from-journals

## Tony's words

> the task should be resumable from debrief journals, even if the sessions are gone.

## What exists today (survey, 2026-11-06)

Each per-client agent ran in a scope that slack-main chose by hand. The only record of the choice is slack-main's handoff journal:

| Client | Scope (project metasmith) | Journals |
|---|---|---|
| Steven Chen | lab/steven-chen | 50+ |
| Rajan | lab/rajan | 10 |
| Beth Davenport | lab/beth-davenport | 18 |
| Kalen Dofher | aspire/dev | 18, shared with project work |
| Rokaiya Shatadru | engine/fix-issues-from-antonio | 11, shared with project work |

A deterministic routine cannot choose a scope by judgment. It needs a fixed map from a Slack seat to a scope.

## Already decided in redux (users.md, 2026-11-02)

Tony: "search redux journals about seats, this was decided."

- R128: seats have their own workspace, `agentic-commons`, apart from awm's `agentic-workspace`. Seat work leaves metasmith and awm. Tony: "I dont want this to be pat of metasmith iether" and "I think seats need their own workspace. Maybe we do agentic-commons and every node that holds seats has it?"
- R128 item 3: `seats/<person>/` is the only split by seat. Only that person's seat writes there.
- R128 item 4: each `seats/<person>/` is a local git repository with DVC and no remote. The nightly Globus backup covers lab seats.
- State on 2026-11-06: the template is a draft at `docs/redux/drafts/agentic-commons` (with `scripts/new-seat.sh`). No `agentic-commons` checkout exists on altair, and no awm project of that name exists.

So a client's agent starts in `agentic-commons/seats/<person>/`, not in a metasmith scope. The open part is where its journals go, because an awm journal post names a project and a scope.

## Decision

Tony, 2026-11-06, to "should agentic-commons be an awm project with one scope per person?": "yes".

1. `agentic-commons` is an awm project. Each person has one scope, `seats/<person>`, whose worktree holds their seat folder (R128 item 3).
2. The new-seat routine starts a client's agent in that scope.
3. The agent journals in that scope. The seat brief tells it to read that scope's journals for context when it needs them.
4. A task stays resumable after its session is gone, because the next agent for that person starts in the same scope and reads the same journals.

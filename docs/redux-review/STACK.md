# redux-review: stack

## Purpose

Review each major awm system's architecture after the first build night, so the build and the redux design (docs/redux/) still agree. Record each settled change as a requirement in its docs/redux area doc.

Procedure, from dfs-interview § Work the stack:

1. Run `stack.py <folder> current` for the top topic. Ask only its own questions.
2. Dispatch read-only subagents to survey what already exists for the topic.
3. Record each difference between the survey and the user's description.
4. Ask the user about each difference with an unclear answer.
5. Put one question per message to the user, or a small set of closely related questions. Give your recommendation with it.
6. Queue the other questions with `stack.py <folder> ask`.
7. Record each answer in the topic's `README.md`. Copy the user's words from `quote.py`.
8. Write the result as the current design.
9. Tick each answered question with `stack.py <folder> answer`.
10. Run `stack.py <folder> close` when the topic's question queue is empty.
11. Commit.
12. Run `reflection(verb="compact", args={followup:...})`. Write a followup that invokes `dfs-interview` by name. Name the discussion folder, the closed topic, its commit and the new top topic in it.

## Current topic

**journals**. Its result: `journals/README.md`.

## Stack

- **journals** (2026-11-03): for journals, I want to talk about making journals commentable (threads, like reddit posts). also want to talk about adding tags to journals with a set ontology (by the agents. 1 degree of separate helps with consistency since harder to just add rando tags). tags for existing uses, but then also would allow for complaint posts like bug reports. then journals need a way of also marking "closed", kind of like issues.  This would allow meta imrpovements to be more systematized in awm
  - [x] Tag descriptions. Tony: "each tag should also get a description when adding and is editable" (answered 2026-11-03)
  - [ ] Precedence folds into the journal. Tony: "and prescedent can then fold into the same journal system once updated?"
  - [ ] superseded_by in the journal. Tony: "how does superseded_by work in new journal system?"
  - [ ] Precedence protocol moves to a skill, skills in a folder. Tony: "lets make the tag precedent. we can move the protocol to a skill?" and "we should move the debrief and precedent skills to a folder maybe"
  - [ ] Usage decays with no sweep. Tony: "can we make usage decay so that it doesnt count up forever? can we make this decay not require periodic sweeps?"
  - [ ] Where lineage lives (R13 parent links, R16 graph) once reporting goes: operations, journal or reflection. Raised by redux-harness; follows from queue.md R145 removing reporting, where the sessions lane serves lineage and session_parent
- **search-filters** (2026-11-03): can we make parameterized search possible? then we can do search with empty terms and only sql (if that's what hop takes) with source as kb for hop. Then we can also make searching journals possible to filter to only reports etc
  - [ ] kb tool surface: does hop become a search filter so the kb tool goes (Tony: kb becomes a b2b service only for search)
- **fleet-deploy** (2026-11-03): lets go over each of the major systems architecturally to make sure we are still aligned
  - [ ] Which repo is canonical for awm: GitHub (install.md R114, R118) or mizar (routines.md R31)? Today origin is altair.
  - [ ] fleet.md R109: the harness lane proposes item 7, an access-token login and a caller-token door for isolated sessions. Accept it?
- **trust-users** (2026-11-03): lets go over each of the major systems architecturally to make sure we are still aligned

## Parked

## Closed

- **search** (closed 2026-11-03): `search/README.md`
- **scopes-awm** (closed 2026-11-03): `scopes-awm/README.md`
- **brief-inject** (closed 2026-11-03): `brief-inject/README.md`
- **plugin-core** (closed 2026-11-03): `plugin-core/README.md`
- **sessions-harness** (closed 2026-11-03): `sessions-harness/README.md`
- **queue-approver** (closed 2026-11-03): `queue-approver/README.md`
- **channels** (closed 2026-11-03): `channels/README.md`

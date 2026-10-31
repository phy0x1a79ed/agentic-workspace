# Sessions

## Purpose & Contents

This doc holds a session's id, its lifecycle from start to retirement, its journals, and the lineage that links sessions. Thread 1b and thread 1l produced it.

## Requirements

### Identity

- **R9.** One id refers to one session, in awm and on its platform alike. Tony: "we have 1 and only 1 id that references each task/session across claude and awm."
  - The id is hex, of any length. Its first 2 characters name the platform.
  - A Claude Code session's id is `cc` followed by the hex digits of its full Claude session UUID.
  - awm accepts incoming ids. It mints no internal ids that map to them.
  - A short form, such as a unique prefix, is only a lookup on this id.
- **R13.** Each session records the id of the session that started it. A session that clears itself gets a new id, with the old id as its parent. One parent link covers both a clear and a `start_other`.

### Lifecycle

- **R8.** A session closes with the standard agent exit. Retiring stops it fully and leaves a report to whoever started it. The report is the session's journal, written once.
- **R10.** A retired session stays resumable through its transcript, by its session id (R9). The job itself is gone. The default follow-up is still a new session, which starts from the old session's journal only. Tony: "In instances where I do ask for a \"resume\", getting the actual session back would be the best."
- **R11.** Each session deploys its work. It skips the deploy only when no deploy is possible, as for a design draft. Tony: "always deploy. if impossible (like design drafts), dont deploy." The deploy follows the project's instructions. An agent asks Tony when it is unsure.
- **R12.** A session closes on its own. Tony does not accept it first.
  1. When its task is done, the agent debriefs. The debrief writes the journal, which posts to the card. Tony reviews the journal without a live agent.
  2. With every task done and no pending issue, the agent retires.
  3. When the agent finds follow-up issues, it stays live. It raises each one with a blocked or review request, so that it reaches Tony's list. Tony queues new work from what he sees.
  4. Tony's decisions on follow-up issues go into precedence, so that a later awm can dispatch follow-up work on its own.
- Tony starts a new session from a report easily. A button generates a new task that refers to the previous session's report. Its place waits for thread 1d, Tony's interface to the swarm.
- **R18.** A follow-up session routes itself the same way as any new session (R1). Its start carries the previous session's scope as a suggested route. The prompt tells the agent that it may choose another scope when the work does not fit there.

### Journals and lineage

- **R14.** A session writes one or more journals. Each journal records the id of the session that wrote it, in the R9 form. Each journal can cite other journals. A session's citations are the union of its journals' citations. Tony: the R9 form "is largely ceremonial now, but crucial when we add other agentic platforms like deepseek."
- **R17.** A citation points only at a journal. When a source session has no journal, as after a rushed or spot session, the citing agent first writes a journal for that session. It then cites that new journal. This keeps the citation field one type.
  - A journal post takes an optional session id. It defaults to the posting session. An agent omits it in the usual case, and names another session only to journal for that session.
- **R15.** The agent chooses its citations at debrief. awm records every journal and session that a session reads, as the candidate list. The agent cites only the sources that shaped its work, each with a one-line reason. Tony: "lets use the debrief."
- **R16.** The lineage graph has two kinds of edge. A parent link (R13) is a hard edge, "started by". A citation (R14) is a soft edge, "built on". The idea came from John's swarm on shaula.

**CAUTION** awm cannot build citations from journal reads alone. Agents also read journals that turn out irrelevant.

## Decisions

- 2026-10-30: New work goes to a new session, never into a live session's context. Tony: "I feel this encourages better performance from agents." See R3 for a scope that a live session occupies.
- 2026-10-30: A result that is not good gets a new session, not a restart of the old session with its full context.
- 2026-10-30: Agents use subagents freely. They create top-level sessions only with Tony's permission.
- 2026-10-30: Seats are sessions. A seat is a session on a channel.

## Gap from today

Surveyed read only on 2026-10-30 and 2026-10-31.

- **R8 partial.** The `retire` skill debriefs, reports to the starter, then runs `reflection stop`, which deletes the job. A close takes 2 to 4 writes: a journal post, a `request_human` review summary, and a message to the parent or a `social send`. Journal posts already copy onto the session's card.
- **R9 partial.** Four ids exist: the 8-hex job id, the Claude session UUID, the card's Trilium id, and the journal post UUID. A Claude Code job id is the first 8 hex digits of the session id it started with. The reporting ledger mints integer task ids, keyed by node plus job, and keeps session ids as aliases (`reporting/ledger.py:1-8`). No button starts a task from a report. Discord `/new-agent` starts a fresh session in `awm/dev` from a free-text prompt, and it takes no report reference (`reporting/commands.py:136`). Board cards carry no action buttons. Moving a card between columns is the only action on the board.
- **R10 conflicts.** Retire removes the job, so `resume_other` refuses it. `start_other replaces` seeds the new session from the last 8 transcript turns, and reads the old job's state, which retire deletes.
- **R11 exists.** The rule today is "always deploy" (precedence `39821f68`).
- **R12 mostly exists.** The agent decides it is done. A review summary followed by retire lands the card in Done (commit `46b01ace`). Precedence `88fd2da392529ca3` keeps an agent alive while its questions are open.
- **R14 partial.** The journal service mints a random UUID for each post (`journal/channel.py:234`). The gateway stamps `meta.session` with the posting session's raw Claude UUID, not the R9 form, and drops a caller's own value (`journal/operations/posts.py`, `_with_session`). Nothing limits the posts per session. No citation field exists. `meta` is free-form.
- **R15 missing.** awm does not record which journals a session reads.
- **R17 missing.** `_with_session` always stamps the posting session, so a journal cannot name another session as the one it covers.
- **R18 conflicts.** `cx start` requires a project and a scope, and places the session there (`cx/lifecycle.py`). `replaces` keeps the caller's scope. Nothing passes a scope as a suggestion only.

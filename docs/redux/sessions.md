# Sessions

## Purpose & Contents

This doc holds a session's id, the id of its task, its lifecycle from start to retirement, its journals, the lineage that links sessions, how a session waits on the sessions it depends on, and what a seat session does. Threads 1b, 1l, 1c and 1e produce it.

## Requirements

### Identity

- **R9.** One id refers to one session, in awm and on its platform alike. Tony: "we have 1 and only 1 id that references each task/session across claude and awm."
  - The id is hex, of any length. Its first 2 characters name the platform.
  - A Claude Code session's id is `cc` followed by the hex digits of its full Claude session UUID.
  - awm accepts incoming session ids. It mints no session ids of its own that map to them. A task has its own id (R58).
  - A short form, such as a unique prefix, is only a lookup on this id.
  - The session id and the task id (R58) are the only keys awm tracks by. Tony: "The real tracking is the 2 character claude indicator plus claude session id for sessions and the task ids minted by awm" (2026-11-01).
- **R56.** The sessions of one task share a common prefix in their names, so that Tony can tell them apart. A name serves legibility only. awm tracks sessions by session id (R9) and tasks by task id (R58), never by name. Tony: "We also need to give guidelines for naming... sessions for a task needs to be identifiable. Use common prefix.", "Id  prefer human readable prefix and session descriptor but fear will get long" and "lets omit the id in the name. Names can be for legibility only." (2026-11-01).
  1. The task name is a slug of 2 or 3 words, such as `redux-naming`. The coordinator takes the task name.
  2. Each session it starts takes `<task>/<descriptor>`, where the descriptor names the concern in 1 to 3 words, such as `redux-naming/survey`.
  3. A name stays under about 40 characters. The brief states this as a guideline. Code does not check it.
  - Precedence `f7def9a0d79ac72f` records this format.
- **R58.** awm mints one task id for each card. The task id points to every session of the task, active or finished. Tony: "we should use real ids since the task slug may not have enough uniquiness", "Since a task can have multiple sessions, the id is no longer 1 to 1 so awm needs to manage this now" and "it would be easier if awm just mints a task id per card, which points to multiple sessions, active or otherwise" (2026-11-01).
  - The task id is the first 8 characters of a random UUID in URL-safe base64, with `-` and `_`. Tony: "Lets expand the characters to 8 and use uuids converted to base64" (2026-11-01).
  - The URL-safe alphabet keeps the id usable in a URL or a path. The standard alphabet holds `/`.
- **R57.** A session reports to the card of its task. awm joins a session to a task by the task id. Tony: "So now we we want a card/task to track potentially multiple sessions" and "This looks good" (2026-11-01).
  1. A session that starts new work gets a new card. awm mints the task id then.
  2. `reflection whoami` reports the session's task id.
  3. The coordinator passes the task id when it starts a session. awm puts that session on the existing card.
  4. The coordinator renames itself to the task name with `reflection rename`, for legibility (R56).
  5. A session that joins a running task, as in R52 path 3, sets its task id to the running task's id with one verb. Its own card folds into the task's card. awm tells the task's main session that it joined. The main session is the coordinator, or the one session of a simple task. Tony: "Lets aim for b actually. The coordinator or main session should know when thjs happens" (2026-11-01).
- **R13.** Each session records the id of the session that started it. A session that clears itself gets a new id, with the old id as its parent. One parent link covers both a clear and a `start_other`.

### Lifecycle

- **R8.** A session closes with the standard agent exit. Retiring stops it fully and leaves a report to whoever started it. The report is the session's journal, written once.
- **R10.** A retired session stays resumable through its transcript, by its session id (R9). The job itself is gone. The default follow-up is still a new session, which starts from the old session's journal only. Tony: "In instances where I do ask for a \"resume\", getting the actual session back would be the best."
- **R11.** Each session deploys its work. It skips the deploy only when no deploy is possible, as for a design draft. Tony: "always deploy. if impossible (like design drafts), dont deploy." A session that a coordinator started for a complex task leaves its branch to the coordinator, which deploys (R52 path 2). The deploy follows the project's instructions. An agent asks Tony when it is unsure.
- **R12.** A session closes on its own. Tony does not accept it first.
  1. When its task is done, the agent debriefs. The debrief writes the journal, which posts to the card. Tony reviews the journal without a live agent.
  2. With every task done and no pending issue, the agent retires.
  3. When the agent finds follow-up issues, it stays live. It raises each one with a blocked or review request, so that it reaches Tony's list. Tony queues new work from what he sees.
  4. Tony's decisions on follow-up issues go into precedence, so that a later awm can dispatch follow-up work on its own.
- Tony starts a new session from a report easily. An action button on the selected card generates a new task that refers to the previous session's report (`queue.md` R63).
- **R18.** A follow-up session routes itself the same way as any new session (R1). Its start carries the previous session's scope as a suggested route. The prompt tells the agent that it may choose another scope when the work does not fit there.

### Journals and lineage

- **R14.** A session writes one or more journals. Each journal records the id of the session that wrote it, in the R9 form. Each journal can cite other journals. A session's citations are the union of its journals' citations. Tony: the R9 form "is largely ceremonial now, but crucial when we add other agentic platforms like deepseek."
- **R17.** A citation points only at a journal. When a source session has no journal, as after a rushed or spot session, the citing agent first writes a journal for that session. It then cites that new journal. This keeps the citation field one type.
  - A journal post takes an optional session id. It defaults to the posting session. An agent omits it in the usual case, and names another session only to journal for that session.
- **R15.** The agent chooses its citations at debrief. awm records every journal and session that a session reads, as the candidate list. The agent cites only the sources that shaped its work, each with a one-line reason. Tony: "lets use the debrief."
- **R16.** The lineage graph has two kinds of edge. A parent link (R13) is a hard edge, "started by". A citation (R14) is a soft edge, "built on". The idea came from John's swarm on shaula.

**CAUTION** awm cannot build citations from journal reads alone. Agents also read journals that turn out irrelevant.

### Dependencies

- **R51.** A session that depends on other sessions waits until they finish. The asking session starts the top-level sessions for the larger issue, as `dispatch.md` R50 allows. It then asks awm to make it wait until they finish. Each started session reports with its journal and retires (R8, R12). It does not wait on anything. Tony: "a session delivers its report as a journal and then retires. it does not wait. this was discussed already" and "so we need a pause mechanism of sorts. agents start other top level sessions when asked, and then asks awm to pause them until they finish" (2026-11-01).
  - A wait works like a retire. Its last step calls `reflection wait` in place of `reflection stop`. The verb's one parameter is `until`, a required list of session ids (R9). awm wakes the waiting session after every listed session finishes. Tony: "pause should function like retire", "no until would go in reflection stop -> need new verb reflection pause_until" and "lets do reflection wait, and have until be the name of the (now non optional) parameter. pause -> wait, I think this is clearer and fits instructions better: "wait until they finish"" (2026-11-01).
  - `reflection wait` waits on sessions only. A coordinator waits on a deploy through the deploy call itself (`routines.md` R26). Tony: "pause is strictly for sessions." (2026-11-01).
  - The wake resumes the session itself from its transcript (R10).
  - The session compacts before it calls `reflection wait`. This is a soft instruction, not a check. Tony: "would be good as soft instructions to compact before retiring with "until" set" (2026-11-01).
- **R52.** Three standard paths cover work that spans sessions. R51 serves the second and the third. Tony: "we should also try to keep things simple and not add new features until necessary." (2026-11-01).
  1. **A simple task.** The agent calls `deploy` and retires. Tony: "an agent does a simple task that requires deploy, calls deploy and retires."
  2. **A complex task.** The coordinating agent starts one top-level session for each concern (`dispatch.md` R50). Each started session moves into its own scope with `reflection cd`. The coordinator waits on them with `reflection wait` (R51). Each started session finishes on its own branch and retires without a deploy. The coordinator merges their branches into its own feature branch, so all of it lands with one `deploy`. Work that covers several projects gets one `deploy` for each project. Tony: "I think in this case the top level agent should coordinate the merging into their feature branch and so all of it gets 1 deploy. The exception is when work covers multiple projects, then the top level agent calls deploy on each." and "i think what im after is actually top level sessions" (2026-11-01).
  3. **Several top-level agents.** Tony starts an agent while an earlier task still runs, because the scope grew or for another reason. The agents coordinate, and one waits on the others with `reflection wait` (R51). Each later agent joins the running task by its task id (R57 step 5). Tony: "I start an agent after an existing task is already running, because I realize the scope is expanded or for whatever reason I get multiple top level agents coordinating. In such cases, an awm wait until [session1, session2, etc] would be useful. Ideally this doesnt happen but real work is messy so that's why we add this so we can ahve the tools ready when needed."

### Seats

- **R67.** A seat is one long-lived session on its channel, such as Tony's Discord DM or a Slack DM. It chats, answers quick questions and helps Tony work the queue when he asks. Each real request becomes a task with its own card and session, started as `dispatch.md` R50 allows. The seat does no task work in its own context. The seat itself has no card. Tony: "the seats then become sessions... I think then socials seats fit into the model completely" and "discord DM also becomes a seat that I can talk to and it is just a channel. the agent should be capable (has access and tooling) to help me with the core pending task/triage list if I ask" (2026-10-30), and "I accept your recommendation. Nothing much changes with seats, its more the stufd around it that changes." (2026-11-01).
- **R68.** A seat acts on the queue only through Tony's passkey. It reads, summarizes and ranks the queue for Tony, and drafts answers. For an action that stands for Tony's approval, such as close, reject, spawn an agent or answer a blocked session, it sends Tony the card's link in the chat. One tap opens the mizar panel (`queue.md` R65), and the passkey confirms the action (`queue.md` R64). The seat is a trusted agent and its platform is not, even on Tony's own trusted DM (`channels.md` R71). It starts its own tasks as `dispatch.md` R50 allows. Tony: "Yes, this is fine. Agents are trusted." (2026-11-01).
- **R78.** Each box checks its own seats. A seat counts as down only while its box has a working internet connection. A box that lost its connection raises no seat alarm. Tony: "Seats also get constant false alarms for when they are down. This was patched but needs fixing. I think the check needs to be local and juat check if the box's internet is connected." (2026-11-01).

## Decisions

- 2026-10-30: New work goes to a new session, never into a live session's context. Tony: "I feel this encourages better performance from agents." See R3 for a scope that a live session occupies.
- 2026-10-30: A result that is not good gets a new session, not a restart of the old session with its full context.

## Gap from today

Surveyed read only on 2026-10-30, 2026-10-31 and 2026-11-01.

- **R8 partial.** The `retire` skill debriefs, reports to the starter, then runs `reflection stop`, which deletes the job. A close takes 2 to 4 writes: a journal post, a `request_human` review summary, and a message to the parent or a `social send`. Journal posts already copy onto the session's card.
- **R9 partial.** Four ids exist: the 8-hex job id, the Claude session UUID, the card's Trilium id, and the journal post UUID. A Claude Code job id is the first 8 hex digits of the session id it started with. The reporting ledger mints integer task ids, keyed by node plus job, and keeps session ids as aliases (`reporting/ledger.py:1-8`). No button starts a task from a report. Discord `/new-agent` starts a fresh session in `awm/dev` from a free-text prompt, and it takes no report reference (`reporting/commands.py:136`). Board cards carry no action buttons. Moving a card between columns is the only action on the board.
- **R10 conflicts.** Retire removes the job, so `resume_other` refuses it. `start_other replaces` seeds the new session from the last 8 transcript turns, and reads the old job's state, which retire deletes.
- **R11 exists.** The rule today is "always deploy" (precedence `39821f68`).
- **R12 mostly exists.** The agent decides it is done. A review summary followed by retire lands the card in Done (commit `46b01ace`). Precedence `88fd2da392529ca3` keeps an agent alive while its questions are open.
- **R14 partial.** The journal service mints a random UUID for each post (`journal/channel.py:234`). The gateway stamps `meta.session` with the posting session's raw Claude UUID, not the R9 form, and drops a caller's own value (`journal/operations/posts.py`, `_with_session`). Nothing limits the posts per session. No citation field exists. `meta` is free-form.
- **R15 missing.** awm does not record which journals a session reads.
- **R17 missing.** `_with_session` always stamps the posting session, so a journal cannot name another session as the one it covers.
- **R18 conflicts.** `cx start` requires a project and a scope, and places the session there (`cx/lifecycle.py`). `replaces` keeps the caller's scope. Nothing passes a scope as a suggestion only.
- **R51 missing.** Nothing makes a session wait until other sessions finish, or resumes it when they do. `retire` is a skill, not a service verb (`~/.claude/skills/retire`). Its last step is `reflection stop`, which runs `claude rm` and deletes the job. No `reflection wait` verb exists. `resume_other` refuses a removed job (R10 conflict). The lifecycle brief that `cx start` adds tells a started session to message its parent before it retires (`REPORT_TO_PARENT`, `cx/lifecycle.py`). The message goes through `SendMessage`, or `reflection message`, which types it into the parent's prompt. It reaches the parent only while the parent's job runs. `retire` deletes a job, and the Claude Code daemon does not restart jobs after a host reboot (parked input).
- **The board tracks waits on Tony only.** The reporting ledger's `waits` table holds jobs that wait on Tony in their own session (`reporting/ledger.py`). A card moves to Blocked only through `request_human`. No state says that a session waits on other sessions.
- **Two delivery paths exist.** `board_watch` types one line into a session for each event on a watched card. The reflection resume after a compaction is a guaranteed delivery of one prompt, with retries and an unbounded wait (`reflection/inject.py`). Either could carry a resume.
- **R56 partial.** A name is free text of at most 80 printable characters (`start_args` in `cx/lifecycle.py`, `rename` in `reflection/control.py`). `cx start` names a session after its scope when the caller gives no name. Nothing sets a prefix for the sessions of one task. Seats already share one, such as `seat:slack/DM/<person>`. A card takes its title from the session name or the plan heading (`reporting/hub_adapter.py`).
- **R57 and R58 missing.** The reporting ledger mints an integer task id for each session, keyed by node plus job (`reporting/ledger.py`). Each card tracks one session. No task id spans sessions. No verb joins a session to another task, and nothing folds one card into another.
- **R67 partial.** The `social` keeper keeps each seat as a pinned session. It resumes the session whenever it ends, and starts a new one only when the seat's brief changes (`social/keeper.py`). One session can hold many chats: `slack-main` holds Tony's Slack DM, 7 member DMs and Slackbot. The `opal-seat-holder` skill already hands each real task to a per-person agent. That agent is pinned and long-lived, one per person, not one per task. The ledger skips pinned sessions (`reporting/tasks.py` `_workers`), so lab work reaches no card.
- **Discord seats run on deneb.** Two Discord seat agents, Tony's DM `tony-dm` among them, run on deneb with the full MCP surface (`users.md`). deneb is now a station that holds no key or token for any other node (`fleet.md`), so these seats move to another node. Thread 3a picks the home of social and the seat agents.
- **R78 broken.** The keeper runs inside `social` on one node and keeps the seats of every node. Each tick asks cx on each seat's node over the network (`social/keeper.py`). A seat down longer than its `alert_after` sends one message to Tony's DM. Tony gets frequent false alarms, after an earlier patch.
- **R68 missing.** A seat agent reaches the board through the same tools as any agent, with no confirmation by Tony. The approval panel of `queue.md` R65 does not exist.
- **No deploy ticket.** The `routine` service is not built, so the ticket of `routines.md` R26 and its wait do not exist.

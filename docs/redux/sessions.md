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
- **R57.** A card follows the sessions of its task, and no session reports to it (`queue.md` R61 item 2). awm joins a session to a task by the task id. Tony: "So now we we want a card/task to track potentially multiple sessions" and "This looks good" (2026-11-01).
  1. A session that starts new work gets a new card. awm mints the task id then.
  2. `reflection whoami` reports the session's task id.
  3. The coordinator passes the task id when it starts a session. awm puts that session on the existing card.
  4. The coordinator renames itself to the task name with `reflection rename`, for legibility (R56).
  5. Tony joins a session to a running task, as in R52 path 3, with the prompt `claim task <id>`. cch's prompt hook makes the claim, and the session calls nothing. A job keeps its card across a `/clear`: the new session takes the old one's place. Tony: "Job keeps its card" (2026-11-09). awm tells the task's main session that it joined. The main session is the coordinator, or the one session of a simple task. Tony: "Lets aim for b actually. The coordinator or main session should know when thjs happens" (2026-11-01).
- **R13.** Each session records the id of the session that started it. A session that clears itself gets a new id, with the old id as its parent. One parent link covers both a clear and a `start_other`.

### Lifecycle

- **R8.** A session closes with the standard agent exit. Retiring stops it fully and leaves a report to whoever started it. The report is the session's journal, written once.
  - The retire instructions state that the last step is `reflection stop`. A session that ends its work and sits idle never wakes the sessions that wait on it (R51). Tony: "retire then explicitly state that reflection stop must be called. a session that just ends and sits would be detrimental." (2026-11-03).
- **R10.** A retired session stays resumable through its transcript, by its session id (R9). The job itself is gone. The default follow-up is still a new session, which starts from the old session's journal only. Tony: "In instances where I do ask for a \"resume\", getting the actual session back would be the best."
  - A retire deletes the job. The session is finished and frees its scope.
  - A wait (R51) keeps the job, because the session comes back. The wake resumes that job.
  - A resume of a retired session starts a new job from its transcript, with the same session id.
  - Tony: "Yes" (2026-11-03), to keeping the job only for a wait.
- **R146.** A session that `start_other` starts has Remote Control on unless the caller turns it off. Its Remote Control name is the session's name, with each `/` written as `-`. Tony: "reflection start_other defaults to remote control on" (2026-11-04).
  - A seat's session follows its seat's Remote Control switch instead, which starts off (`fleet.md` R147 item 7).
  - A switch persists. cch records each switch on the job's lineage record, and a resume starts the session with it. Claude Code's daemon relaunches a job with the flags it was created with. After a relaunch, cch compares the bridge with the record on each bridge change for 2 minutes, and once at 20 s. It puts the switch back at most once, so a later `/rc` in the terminal stands. Tony: "when Claude Code restarted and the session restarted, it shouldn't enter remote control because that was toggled off" (2026-11-09).
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
  - Only a debrief journal cites. Its citations are its `reply_to` list (`fleet.md` R107 item 5). A citation points one way, from the citing journal, and search finds what a session used. Tony: "citations only needed for debrief journals, but we are using the reply_to field so that's fine for comments, etc." and "I do not want links both directions. search uncovers what the agent needed that session." (2026-11-03).
- **R17.** A citation points only at a journal. When a source session has no journal, as after a rushed or spot session, the citing agent first writes a journal for that session. It then cites that new journal. This keeps the citation field one type.
  - A journal post takes an optional session id. It defaults to the posting session. An agent omits it in the usual case, and names another session only to journal for that session.
- **R15.** The agent chooses its citations at debrief. awm records every journal and session that a session reads, as the candidate list. The agent cites only the sources that shaped its work. A citation carries no reason, only the cited journal's id. Tony: "lets use the debrief." and "No reason" (2026-11-03).
  - A read is a `journal fetch`, or a `search get` that opens a journal post. A transcript read is not a candidate, because a citation points only at a journal (R17).
  - The brief that the harness injects (`scopes.md` R143) and a `brief_read` are not candidates. Tony: "makes sense. but since context are jurnals and are injeted, they  should effectively stay out of this" (2026-11-03).
- **R16.** The lineage graph has two kinds of edge. A parent link (R13) is a hard edge, "started by". A citation (R14) is a soft edge, "built on". The idea came from John's swarm on shaula.
  - The journal holds the lineage. `reflection` records each parent link there when it starts a session or a session clears itself. The journal's `lineage` verb walks both kinds of edge forward, from a session to its parent and to the journals it cites. Tony: "ok, reply to should accept a list of previous journals used" (2026-11-03).

**CAUTION** awm cannot build citations from journal reads alone. Agents also read journals that turn out irrelevant.

### Dependencies

- **R141.** A lost compaction or follow-up is never silent. reflection keeps each owed follow-up on disk until the transcript shows the session took it up. When the Claude Code daemon swaps a worker during `/compact`, reflection follows the session to its new process and sends again. Each give-up reaches the session, its scope's journal and the log. Tony: "it looks like compaction fails sometimes. Altair compaction failed for the search session, which isbwhy it was never delivered. We gotta investigate and fix this." and "Deploy it now" (2026-11-03).
- **R51.** A session that depends on other sessions waits until they finish. The asking session starts the top-level sessions for the larger issue, as `dispatch.md` R50 allows. It then asks awm to make it wait until they finish. Each started session reports with its journal and retires (R8, R12). It does not wait on anything. Tony: "a session delivers its report as a journal and then retires. it does not wait. this was discussed already" and "so we need a pause mechanism of sorts. agents start other top level sessions when asked, and then asks awm to pause them until they finish" (2026-11-01).
  - A wait works like a retire. Its last step calls `reflection wait` in place of `reflection stop`. The verb's one parameter is `until`, a required list of session ids (R9). awm wakes the waiting session after every listed session finishes. Tony: "pause should function like retire", "no until would go in reflection stop -> need new verb reflection pause_until" and "lets do reflection wait, and have until be the name of the (now non optional) parameter. pause -> wait, I think this is clearer and fits instructions better: "wait until they finish"" (2026-11-01).
  - `reflection wait` waits on sessions only. A coordinator waits on a deploy through the deploy call itself (`routines.md` R26). Tony: "pause is strictly for sessions." (2026-11-01).
  - The wake resumes the session's kept job (R10).
  - A listed session counts as finished only when it retires. A session that itself waits is not finished, so a wait holds through every level of nesting.
  - A terminal session may wait. Tony: "Agree on both" (2026-11-03).
  - The retire triggers the wake. When a session retires, `reflection stop` reads the waits that list it and wakes each waiter whose listed sessions have all retired. No loop polls the waits. Tony: "we dont want sweep, we want active triggers from reflection stop" (2026-11-03).
  - A worker on another node wakes its waiter through reflection on the waiter's node.
  - A failed wake raises an alarm at once and keeps the wait. It does not retry.
  - At start, reflection reads the waits once. It raises one alarm for each wait that lists a session that is neither live nor retired, such as a worker lost to a reboot. An agent or Tony recovers it by hand.
  - A wait clears when its waiter wakes, is resumed by hand, or retires. Tony: "otherwise agree" (2026-11-03).
  - The session compacts before it calls `reflection wait`. This is a soft instruction, not a check. Tony: "would be good as soft instructions to compact before retiring with "until" set" (2026-11-01).
- **R52.** Three standard paths cover work that spans sessions. R51 serves the second and the third. Tony: "we should also try to keep things simple and not add new features until necessary." (2026-11-01).
  1. **A simple task.** The agent calls `deploy` and retires. Tony: "an agent does a simple task that requires deploy, calls deploy and retires."
  2. **A complex task.** The coordinating agent starts one top-level session for each concern (`dispatch.md` R50). Each started session moves into its own scope with `reflection cd`. The coordinator waits on them with `reflection wait` (R51). Each started session finishes on its own branch and retires without a deploy. The coordinator merges their branches into its own feature branch, so all of it lands with one `deploy`. Work that covers several projects gets one `deploy` for each project. Tony: "I think in this case the top level agent should coordinate the merging into their feature branch and so all of it gets 1 deploy. The exception is when work covers multiple projects, then the top level agent calls deploy on each." and "i think what im after is actually top level sessions" (2026-11-01).
  3. **Several top-level agents.** Tony starts an agent while an earlier task still runs, because the scope grew or for another reason. The agents coordinate, and one waits on the others with `reflection wait` (R51). Each later agent joins the running task by its task id (R57 step 5). Tony: "I start an agent after an existing task is already running, because I realize the scope is expanded or for whatever reason I get multiple top level agents coordinating. In such cases, an awm wait until [session1, session2, etc] would be useful. Ideally this doesnt happen but real work is messy so that's why we add this so we can ahve the tools ready when needed."

### Seats

- **R67.** A seat is one long-lived session on its channel (its lifecycle is R149), such as Tony's Discord DM or a Slack DM. It chats, answers quick questions and helps Tony work the queue when he asks. Each real request becomes a task with its own card and session, started as `dispatch.md` R50 allows. The seat does no task work in its own context. The seat itself has no card. A cooperative seat makes no cards (`channels.md` R125 item 5). Tony: "the seats then become sessions... I think then socials seats fit into the model completely" and "discord DM also becomes a seat that I can talk to and it is just a channel. the agent should be capable (has access and tooling) to help me with the core pending task/triage list if I ask" (2026-10-30), and "I accept your recommendation. Nothing much changes with seats, its more the stufd around it that changes." (2026-11-01).
- **R68.** A seat acts on the queue only through Tony's passkey. It reads, summarizes and ranks the queue for Tony, and drafts answers. For an action that stands for Tony's approval, such as close, reject, spawn an agent or answer a blocked session, it sends Tony the card's link in the chat. One tap opens the approver panel on alcor (`queue.md` R65), and the passkey confirms the action (`queue.md` R64). The seat is a trusted agent and its platform is not, even on Tony's own trusted DM (`channels.md` R71). It starts its own tasks as `dispatch.md` R50 allows. Tony: "Yes, this is fine. Agents are trusted." (2026-11-01).
- **R78.** Each box checks its own seats. A seat counts as down only while its box has a working internet connection. A box that lost its connection raises no seat alarm. Tony: "Seats also get constant false alarms for when they are down. This was patched but needs fixing. I think the check needs to be local and juat check if the box's internet is connected." (2026-11-01).
- **R149.** A seat is a declared chat that awm watches. A social account has channels, and a channel may have a seat. A seat may have no live agent. Its agent is a persistent session with no board and nothing to mark done. Tony: "the seed is persistent, but the agent session doesn't need to retire. It also doesn't get a board, so we need to remove the things like mark done" and "A channel may not necessarily have a seat. If it has a seat, AWM is monitoring it. A channel that has a seat doesn't necessarily have a live agent." (2026-11-09).
  1. **Start.** A message to a seat with no agent starts one. The brief asks it to answer each new message, journal after each result or decision, keep its session name and never end the session.
  2. **Idle stop.** The seat stops its agent after 4 hours with no message and no running turn. The next message resumes the same conversation. Remote Control stays as the seat's switch sets it. The retire skill is not part of this. Tony: "Idle shut down, I accept. 4 hours with no message and no running turn. Remote control can stay on." and "Retire is a specific skill. An agent can be shut down by AWM after the 4-hour timeout without explicitly invoking the retire skill." (2026-11-09).
  3. **Notices.** Tony gets a notice on the first failed start of a streak, with the reason and the `seats reset` command, and again when the start breaker holds the seat. He gets one when an agent the seat started ends or stops taking messages. An idle stop and the end of an adopted session send none. Tony: "If the seat doesn't come up in 3 minutes, I should get a notice. I can retry, but I should get a notice on the first failure." (2026-11-09).
  4. **Pause.** A message to a seat while seats are paused gets no agent. The next agent's first prompt gives the count and points at `social history`. Tony: "Dropping message while paused is fine if after unpause agents can see the chat history." (2026-11-09).
  5. **Adopt.** `seats adopt` on the seat's host is the one way to hand a running session a declared seat. `social mount` refuses a declared seat.
  6. **Journals.** The agent journals in its seat scope, tagged `journal`, `seat` and `user:<slug>`. Tony: "Since we are also harmonizing the brief for seeds, we should also encourage journaling." (2026-11-09).

## Decisions

- 2026-10-30: New work goes to a new session, never into a live session's context. Tony: "I feel this encourages better performance from agents." See R3 for a scope that a live session occupies.
- 2026-10-30: A result that is not good gets a new session, not a restart of the old session with its full context.

## Gap from today

Status at the wave-2 gather (`feat/redux` 16da3979, 2026-11-04). `docs/redux-review/build-survey.md` § 3 holds the row for each requirement.

- **R8, R12 item 4.** The `~/.claude` drafts under `docs/redux/drafts/claude-home/` go live at T27. The retire skill ends with `reflection stop` (R8). The debrief skill posts with `tags` and `reply_to`, and writes each user decision as a precedent post (R12 item 4).
- **R51.** The cross-node wake ran against a stub only. The deploy sweep checks it across real nodes.
- **R52.** Its paths need the R26 deploy ticket live on the nodes (`routines.md` R26).

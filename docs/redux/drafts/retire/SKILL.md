---
name: retire
description: End this session for good — run when the user says "retire", "retire this session" or "close this session", or when your work is done. Debriefs, points whoever started the session at the journal, then stops the session, or parks it with reflection wait when it waits on other sessions. Ends the session only, never the scope or its goal.
---

# Retire

Run this when told to retire, or when your work is done. The work is done when what you were asked for is landed and deployed, it is verified, and nothing waits on you. You close on your own. Tony does not accept the work first.

Retire ends the session, not the work. Never run `scope complete`, `goal_retire` or a branch delete as part of it.

Do not retire while you have follow-up issues. Raise each one with `operations request_human` (`kind:"blocked"` or `kind:"review"`) and stay live until Tony answers.

## 1. Debrief

Run the `debrief` skill in full. Its journal is your report, written once. It posts to your task's card, where Tony reads it without a live agent.

At the debrief, cite the journals that shaped your work. Run `journal reads` for the candidates. Cite only the sources that mattered, each with a one-line reason, in the journal's `meta.cites`.

Write no review summary and no second report. The journal is the report.

## 2. Point your starter at the journal

Skip this step when the user gave the word in this session.

Send one line with the journal id and the outcome to whoever started you:

- A session that started you: `SendMessage` it when `ListAgents` lists it, else `reflection message`.
- Discord or Slack, as your first prompt says: one `social send` to the account and channel that prompt names.
- Anything else: the journal on the card is the report. Send nothing.

## 3. Stop what this session started

1. Stop each Monitor, cron and background shell this session started.
2. Name anything you abandoned mid-run in the debrief journal.

## 4. Stop or wait

1. Run `reflection whoami`. It names your session, your job and your task.
2. When you wait on other sessions, compact first. Then run `reflection wait {until:[<their session ids>]}` as your last call. It takes no job and acts on you. It stops you the way `stop` does, and awm resumes you when every listed session has stopped.
3. Otherwise, for a background session, run `reflection stop {job, confirm:true}` as your last call. The job stays resumable by its session id (R10). Never pass `remove:true` when you retire.
4. For a terminal session, run `reflection message_self {text:"/exit", confirm:true}`, then end the turn.

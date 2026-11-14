---
name: retire
description: End this session for good — run when the user says "retire", "retire this session" or "close this session", or when your work is done. Debriefs, points whoever started the session at the journal, then ends with reflection stop, or with reflection wait when it waits on other sessions. Ends the session only, never the scope.
---

# Retire

Run this when told to retire, or when your work is done. The work is done when what you were asked for is landed and deployed, it is verified, and nothing waits on you. You close on your own. Tony does not accept the work first.

Retire ends the session, not the work. Never run `scope complete` or a branch delete as part of it.

Do not retire while you have follow-up issues. Raise each one with `operations request_human` (`kind:"blocked"` or `kind:"review"`) and stay live until Tony answers.

## 1. Debrief

Run the `debrief` skill in full. Its journal is your report, written once, with its citations in `reply_to`. Tony reads it from your task's card without a live agent.

The debrief's hand-back names the journal on your card. Write no second report.

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

Your last call is always `reflection stop` or `reflection wait`. A session that ends its work and sits idle never wakes the sessions that wait on it: only `reflection stop` tells them you retired.

1. Run `reflection whoami`. It names your session and your task.
2. When you wait on other sessions, compact first. Then run `reflection wait {until:[<their session ids>]}` as your last call. It takes no job and acts on you. A background session stops and keeps its job. A terminal session stays open. awm wakes you when every listed session has retired.
3. Otherwise, run `reflection stop {confirm:true}` as your last call. With no `to` it acts on you. A background session stops and its job is removed. A terminal session gets `/exit` typed into it, which runs when the turn ends. Either way the stop frees the scope and wakes the sessions that wait on you. The transcript stays, so a later resume by session id still works.

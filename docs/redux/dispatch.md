# Dispatch

## Purpose & Contents

This doc holds how awm starts work that nobody asked for directly: it notices an issue, matches it against Tony's recorded decisions, and dispatches a job for it. It holds what a decision must record to drive a dispatch, what may trigger one, who has the authority, and how Tony sees and corrects the result. Thread 1k produces it.

Timer and agent triggered processes belong in `routines.md`. How a session starts, closes and raises follow-up issues belongs in `sessions.md`.

## Requirements

None yet. `sessions.md` R12 step 4 already asks for the input: Tony's decisions on follow-up issues go into precedence.

## Decisions

None yet.

## Gap from today

Surveyed read only on 2026-10-31, from the `feat/redux` source and the live precedence archive on altair.

- **No automatic dispatch.** Only `cx start` starts a background session (`cx/lifecycle.py`). Its callers are Tony's Discord `/new-agent`, an agent's `reflection start_other`, and seeded starts (`cx/seed.py`). Each start comes from a person or an agent turn. No event, condition or decision starts one.
- **Precedence is read by agents, never by awm.** Agents search it before they ask Tony a preference question. No service reads it to act.
- **Decisions are free text.** A decision is a triple of context, question and decision, with tags, notes and votes (`precedence/db.py`). It has no field for an issue class, an action to take, a target scope, or a scope of authority. Search ranks by embedding relevance, recency and votes.
- **Every decision is an agent's paraphrase.** The archive holds 105 active decisions, and each has source `agent`. Agents write them on Tony's behalf, usually with his quote inside. None are written by Tony or captured from his answer directly.
- **Answers on the board are not recorded.** An answer to `operations request_human` reaches the asking session only. Nothing adds it to precedence unless that session does.
- **The scrapers stage candidates only.** `precedence/scrape.py` and `scrape_transcripts.py` mine memories, operator posts, journals and transcripts into a manifest that a person curates before import. They never write the archive.
- **Schedulers do not start agents.** The `events` service emits `schedule.tick` and `agent.wake`, and only the game bot consumes them. The `routine` service of `routines.md` R34, which tests conditions and dispatches agents, is designed but not built.
- **The standing rule asks for permission.** Thread 1e records that agents create top-level sessions only with Tony's permission. Automatic dispatch needs an exception to it, or a standing permission that a decision grants.

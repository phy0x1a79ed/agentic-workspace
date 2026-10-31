# Users

## Purpose & Contents

This doc holds who uses awm, through which channel, and what each user can reach and must never touch. It also holds the Slack seat system. Thread 3 and thread 3a fill it. Nothing here is settled yet. The survey below is the current state, and it waits for Tony's own view.

## Today

Tony: "me and also lab members through slack seats on altair, under my supervision" (2026-10-30).

Survey of current use, read only on 2026-10-30. Users beyond Tony and the lab:

- A second Arbutus admin approves admin windows by Slack reply.
- A collaborator's swarm, peer `shaula`, holds read grants on journals, kb, notes, precedence and writing.
- Bots arrive through Discord kept seats.
- altair's HTTPS edge has 2 static users, not yet identified.
- People who accept a tether session reach an agent from their own computers.

## The Slack seat system today

- Placement:
  - The `social` service and the seat config live on mira.
  - Slack seat agents run on altair. Discord seat agents run on deneb.
  - Kept seats in mira's `social.toml` start and replace the agents through cx.
- One session, `slack-main`, holds Tony's DM and 7 member DMs. 5 members have their own agents.
- Supervision is by exception. Agents call `request_human` for costly, irreversible or permission questions (precedence `89e3306ab5f9656f`, `1f7107b1750152f3`).
- Restrictions on a seat are prose only:
  - Seat agents run in mode `worker` with skip-permissions and the full MCP surface. Tony's personal accounts are reachable.
  - An unscoped `social history` returns every chat, Tony's DM included.
- Drift:
  - mizar's copy of the `opal-seat-holder` skill is out of date.
  - Two lab agents run in scopes not tagged `lab`.
  - `slack-main` runs in the `awm/dev` worktree.
  - Editing a brief replaces the agent and loses its context.

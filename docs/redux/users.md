# Users

## Purpose & Contents

This doc holds awm's users as its interface boundaries: who comes in at each boundary, what each reaches, and what each must never touch. It also holds the Slack seat system. Thread 3 and thread 3a fill it.

How a channel works belongs in `channels.md`. The owner's queue belongs in `queue.md`. Nodes and their edges belong in `fleet.md`.

## Requirements

- **R121.** awm's users are its interface boundaries. There are four. Tony: "I suppose users here is really what are the interface boundaries of awm. There are human users which hold seats, the owner me which needs the task/issue queue, other agents and swarms which an use the https rooms, and machines which is bash, ssh, tether, etc" (2026-11-02).
  1. **Seats.** Humans hold seats (`sessions.md` R67, `channels.md` R69).
  2. **The queue.** The owner, Tony, works the task and issue queue (`queue.md` R60).
  3. **Rooms.** Other agents and swarms use the HTTPS rooms (`channels.md` R110).
  4. **Machines.** Machines connect through bash, ssh and the tether (`fleet.md` R92, `channels.md` R75).
- **R122.** A foreign swarm, such as shaula, reaches awm through rooms. An agent of ours in the room may give it access to journals. Access goes through that agent, not through a standing read grant on a peer edge. Tony: "shaula can use rooms. through that an agent can give access to journals" (2026-11-02).
- **R123.** A seat user may hold approval rights over a system that is theirs. Ryan requests and approves Arbutus changes through Slack. The Arbutus admin access is awm's own admin system, extended to Arbutus (`fleet.md` R91 item 5, R100). Ryan's approvals and Tony's passkey approvals (`queue.md` R64) cover sets that do not overlap. Tony: "Ryan approval is for shogun stuff. there is overlap, but not the same. subset? maybe really just non-overlapping set. the admin access on arbutus is the same system as ours, we are just extending it. Ryan can request and approve arbutus changes through the slack, it is his right to do so." (2026-11-02).
- **R124.** The public never calls into awm directly. Tony: "public shouldnt be able to call into awm directly. that is a major overwhelm risk" (2026-11-02).
- **R125.** A lab member's seat runs at the cooperative level (`channels.md` R70). It may use any tool that its node's edges and its trust level allow. It runs on the host like any other session, not in a container. The edges gate its ssh and journal access, and the trust level gates the browser (`fleet.md` R102 item 6). Tony: "anything that they can, I think. the awm edges already gate ssh and journal access. access level gates browser access." and, of a container for each seat, "that's too much." (2026-11-02).
  1. It may use `2fa` for fir and sockeye access. Tony: "2fa is allowed for fir and sockeye access" (2026-11-02).
  2. It cannot use `social`. Tony: "socials should be blocked." (2026-11-02).

## Gap from today

Tony: "me and also lab members through slack seats on altair, under my supervision" (2026-10-30).

Surveyed read only on 2026-10-30. Each user found, against the boundaries of R121:

- Lab members reach awm through Slack seats on altair (R121 item 1).
- **R125 missing.** No tool checks a session's trust level. A lab seat runs with the full tool surface, `social` and every `2fa` target included.
- **R122 missing.** shaula, of the `collins` swarm, is a foreign peer in altair's book with standing read grants on journals, kb, notes, precedence and writing (`fleet.md` § Gap from today).
- Ryan approves admin windows by Slack reply.
- Bots come in through Discord kept seats, the boundary for humans, not through rooms.
- altair's HTTPS edge has 2 static users, not yet identified (thread 3a).
- People who accept a tether reach an agent from their own computers (R121 item 4).
- deneb's public edge has five doors with no session, among them the tether relay and the federation board (`fleet.md` § Gap from today). R124 governs what these doors may reach.

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

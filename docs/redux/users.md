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
- **R126.** The Slack seat system keeps its first-contact flow. A main Slack seat watches for new DMs to Opal. On the first DM from someone other than Tony, it notifies Tony and starts a seat for that person, at the cooperative level (`channels.md` R125). Tony: "the current system works. a main slack seat monitors for new DMs. it notifies me and spawns a seat on first DM by someone else." (2026-11-02).
  1. Opal never messages a person first. The Slack send refuses a DM where the person has never written, in code. Tony: "lets also enforce some small tweaks to make that go smoother. like no reaching out first" (2026-11-02).
  2. A seat never uses what a person told Tony in his own DM (`channels.md` R71 item 2).
  3. A person's replies go to their own seat from the first message. A change to one seat never restarts another.
  4. An instruction in Tony's DM does not spread to the lab seats as a new permission. It reaches a seat only when Tony names that seat.
  5. The main Slack seat handles the edge cases, such as an @Opal mention in a channel or a sender from outside the lab. It uses judgement and may ask Tony. Tony: "slack main handles those edges cases and uses judgement. can also ask me" (2026-11-02).

  Items 1 to 4 come from the Beth case of 2026-10-29: Opal sent Beth a cold plan after an instruction in Tony's DM fanned out to every lab seat. Tony kept these four of six proposed tweaks: "drop 2 / drop 6" (2026-11-02).
- **R128.** The seat SOP lives on altair, the node that holds the Slack seats. It stays out of the awm repo, because it applies only there. Tony: "the seat SOP, lets save that soemwhere on altiar since it only applies there. This prolly shouldnt go in the awm repo." (2026-11-02).
  1. The seats get an awm project on altair. Its repo holds the SOP, so git tracks each change, and altair's skills folder links to that one copy. Tony: "Seats can get a project so that it gets tracked?" and "perfect" (2026-11-02).
  2. The seats project holds every seat's scope: the main seat and one `lab/<person>` scope per person, on the model of Steven Chen's `metasmith/lab/steven-chen`. Seat work leaves metasmith and the awm project. Merges between the seat scopes must not collide. Tony: "Need to be careful so that merges dont collide." and "lets migrate steven's model. I dont want this to be pat of metasmith iether" (2026-11-02).
  3. The seats project's remote is a private repository on the Hallam lab GitHub. Tony: "we can add remote to hallam lab github" and "as private" (2026-11-02).

## Gap from today

Tony: "me and also lab members through slack seats on altair, under my supervision" (2026-10-30).

Surveyed read only on 2026-10-30. Each user found, against the boundaries of R121:

- Lab members reach awm through Slack seats on altair (R121 item 1).
- **R126 broken.** Nothing in `social` stops a first message: `awm/services/social/awm/social/connectors/slack_bot_conn.py` and `hub_adapter.py` send to any DM. A seat brief lives in mira's `social.toml`, and editing one replaces other seats' agents. slack-main relays Tony's instructions to every lab seat.
- **R128 missing.** No seats project exists. Person scopes sit in metasmith (`lab/steven-chen`, `lab/beth-davenport`), seat sessions in awm (`seat-slack-main`, `seat-tony-dm` and others), and the SOP lives as loose skill copies on mizar and altair.
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

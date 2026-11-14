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
- **R122.** Another swarm, such as shaula, reaches awm through rooms. An agent of ours in the room may give it access to journals. Access goes through that agent. No swarm holds a standing read grant (`fleet.md` R86). Tony: "shaula can use rooms. through that an agent can give access to journals" (2026-11-02).
- **R123.** A seat user may hold approval rights over a system that is theirs. Ryan requests and approves Arbutus changes through Slack. His `approve` in the thread of a session's `admin_ssh` request for shogun issues the same grant certificate as Tony's passkey, and the approver refuses one that names any other node. The Arbutus admin access is awm's own admin system, extended to Arbutus (`fleet.md` R91 item 5, R100). Ryan's approvals and Tony's passkey approvals (`queue.md` R64) cover sets that do not overlap. Tony: "Ryan approval is for shogun stuff. there is overlap, but not the same. subset? maybe really just non-overlapping set. the admin access on arbutus is the same system as ours, we are just extending it. Ryan can request and approve arbutus changes through the slack, it is his right to do so." (2026-11-02).
- **R124.** The public never calls into awm directly. Tony: "public shouldnt be able to call into awm directly. that is a major overwhelm risk" (2026-11-02).
- **R126.** The Slack seat system keeps its first-contact flow. A main Slack seat watches for new DMs to Opal. On the first DM from someone other than Tony, it notifies Tony and starts a seat for that person, at the cooperative level (`channels.md` R125). Tony: "the current system works. a main slack seat monitors for new DMs. it notifies me and spawns a seat on first DM by someone else." (2026-11-02).
  1. Opal never messages a person first. The Slack send refuses a DM where the person has never written, in code. Tony: "lets also enforce some small tweaks to make that go smoother. like no reaching out first" (2026-11-02).
  2. A seat never uses what a person told Tony in his own DM (`channels.md` R71 item 2).
  3. A person's replies go to their own seat from the first message. A change to one seat never restarts another.
  4. An instruction in Tony's DM does not spread to the lab seats as a new permission. It reaches a seat only when Tony names that seat.
  5. The main Slack seat handles the edge cases, such as an @Opal mention in a channel or a sender from outside the lab. It uses judgement and may ask Tony. Tony: "slack main handles those edges cases and uses judgement. can also ask me" (2026-11-02).

  Items 1 to 4 come from the Beth case of 2026-10-29: Opal sent Beth a cold plan after an instruction in Tony's DM fanned out to every lab seat. Tony kept these four of six proposed tweaks: "drop 2 / drop 6" (2026-11-02).
- **R128.** Seats have their own workspace, `agentic-commons`, apart from awm's `agentic-workspace`. Every node that holds seats keeps a checkout of it. The seat SOP lives there, out of the awm repo, and seat work leaves metasmith and awm. Tony: "the seat SOP, lets save that soemwhere on altiar since it only applies there. This prolly shouldnt go in the awm repo.", "I dont want this to be pat of metasmith iether" and "I think seats need their own workspace. Maybe we do agentic-commons and every node that holds seats has it?" (2026-11-02).
  1. Its repository lives on Tony's GitHub, with a private fork on the Hallam lab GitHub. Tony: "We can place it on our github and hallam lab forks." and "as private" (2026-11-02).
  2. It holds docs, the SOP among them, DVC-tracked data and the seat folders, on the metasmith layout. Tony: "can we borrow structure of metasmith repo?" and "do seats, docs, dvc tracked data nothing else is needed right?" (2026-11-02).
  3. `seats/<person>/` is the only split by seat, and git ignores it. Only that person's seat writes there, and only the main seat writes outside `seats/`, so merges never collide. Tony: "Need to be careful so that merges dont collide.", "we want to avoid having seat splits down multiple folders, so seats/<person> is the only seat split. others apply to the whole setup" and "The users subfokders  for each seat is gitignored" (2026-11-02).
  4. Each `seats/<person>/` is a local git repository, with DVC for its data. It has no remote. The nightly Globus backup covers a lab seat's folder. A seat outside the lab needs no backup, and the `agentic-commons` docs say so. Tony: "We can do local got repos and dvc for data. This gets backed up by the nightly globus" and "For non lab seats, no backups is fine. This can be described in the docs" (2026-11-02).
- **R129.** Slack seats run on altair. Discord seats move from deneb to mizar, because deneb runs no agents (`fleet.md` R79). `social` moves from mira to alcor with mira's other jobs (`fleet.md` R88 item 1). Tony: "Discord to mizar is fine." (2026-11-02).
- **R130.** awm has no static edge users. An agent or node reaches another node through an ssh edge (`fleet.md` R87) or a singleton's service door (`fleet.md` R98). Tony reaches a gateway portal with his passkey (`fleet.md` R84). No login holds a fixed password. Tony: "This is replaced by the ssh or service edge no? I dont see how this fits with decided infrastructure" (2026-11-03).

## Gap from today

Status at the wave-2 gather (`feat/redux` 16da3979, 2026-11-04). `docs/redux-review/build-survey.md` § 3 holds the row for each requirement.

- **R124.** The board door stays public from the cutover until `feat/redux-r77` merges at T27 (`channels.md` R77).
- **R128.** The seat workspace template exists. Its GitHub repo and the lab fork do not.
- **R129.** The cloud branch `feat/redux-fleet` places the Slack seats on altair and the Discord seats on mizar, and moves social off mira. That config is not applied.
- **R130.** The auth migration to schema v7 removes every password login. It runs at the T26 deploy.

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
  - Two lab agents run in scopes not tagged `lab`.
  - `slack-main` runs in the `awm/dev` worktree.
  - Editing a brief replaces the agent and loses its context.

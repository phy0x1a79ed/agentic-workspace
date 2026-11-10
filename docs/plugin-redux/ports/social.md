# Port plan: social

## Purpose & Contents

This file holds the approved port plan for social and its seats component, from the comms group's plan (task CWPYAtyb), approved by Tony on 2026-11-06. The Remote Control switch comes from the port-comms items of `docs/trilium-design/CONTRACT.md`, approved by plugin-redux/interview on 2026-11-07. The model, effort and Refresh controls come from the plan of awm/seat-refresh, approved by Tony on 2026-11-10. It names only what the Python does not make obvious. The Python at release 49232bf9 is the reference for everything kept. The rules every comms port follows are in `comms.md`.

## Plan

### Accounts, connectors, buckets and verbs

This lands together with the seats component.

Replaces `awm/services/social/` (11,283 lines). It runs on alcor as `awm-plugin@social` with `AWM_DIR=/var/lib/awm/social`.

Surface changes, listed:
- Removed `keepers`, `keep_reset` and `keep_adopt`, the seven OneDrive-only bucket verbs, and `bucket_get`'s `version` (Drive keeps none).
- `bucket_rm` refuses the bucket root.
- Added `forward`, `seat_reset`, `seat_adopt` (on CLI and HTTP) and `seat_admit`.
- `seats` shows each seat's state.
- `send` and `react` ids default to the caller's latest pushed line.
- `send` refuses a DM to someone who never wrote, on Discord too.
- Stale descriptions are fixed.

Connectors and buckets:
- One Slack client serves both the bot and Tony's read-only account.
- Discord uses twilight, keeping `/approve`.
- Gmail uses async-imap, mail-parser and lettre.
- Google Drive is plain REST.
- mira is ignored (Tony: "just ignore mira, do nothing extra"). The mira connector, Teams and the OneDrive bucket are not ported. Their config entries are skipped with a log line and left in the file. The seven OneDrive-only bucket verbs go, as a listed change.
- `awm/services/social/mira/` runs alcor's `awm-sites.service`. The port leaves that folder where it is, untouched, and deletes the rest of `awm/services/social/`.

Borrower mode goes, because the cloud role strips `AWM_SOCIAL_PEER`. `seed.py` goes. `gdrive_auth.py` becomes `social gdrive-auth`.

`social.db` v8 opens in place. v9 adds tables only. The keeper tables stay, unread. `awm/service_components/kept` stays until cch drops its `transcript_tail` import.

social needs `reflection`. It calls `reflection` (`start_other`, `message`, `list_other`, `probe`), `operations request_human` and `journal tag_add`.

Tests: port the non-seat suites against loopback stand-ins, with Gmail attachment fixtures from Python's parser.

Gotchas:
- Slack Socket Mode acks within 3 s.
- The twilight DM-only command payload must equal discord.py's.
- Keep SQLite off the async loop.

### Seats

One module owns every seat in `social.db`: `seat_registry`, a durable `seat_queue`, `seat_routed` for dedupe, and `seat_lines` for the send/react defaults. Each seat's transitions run in one task per seat, and each transition writes the database before it acts.

**Config.** `[seats]` holds `project = "agentic-commons"`, opus and high, `paused`, and `notice`, which defaults to Tony's Discord DM. One `[[seats.rule]]` makes every slack-bot DM a cooperative seat on altair at `seats/{slug}`, except Slackbot. `[[seats.chat]]` overrides name Tony's two DMs (trusted; altair and mizar) and Hallam Hive #general (cooperative, altair). A `[[seats.rule]]` or `[[seats.chat]]` can name its own `model` and `effort`, both or neither, with the values `start_other` takes. A seat that names neither uses `[seats]`. The pair applies at the seat's next agent start.

**Transitions.**
1. **empty**, message: queue it, react 👀, run `ensure_seat` (`scopes create` on the seat's node), then `start_other {project, scope: "seats/<slug>"}` with `peer`. → starting. The rows go into the brief but stay undelivered.
2. **starting**: the agent's first live `list_other` row or social call marks the briefed rows delivered. → occupied. If the agent is not up within 3 min, requeue the rows and count one breaker attempt: 4 per hour, backoff 60, 120, 300 and 600 s, then a 6 h hold. The first failure of a streak and the hold each send a notice. An agent that comes up ends the streak.
3. **occupied**: each message is pushed. A Gone or exhausted push returns its rows → empty, then step 1.
4. **occupied**, 4 h with no message and no running turn: `stop_other` the agent → empty, with the job marked stopped. The clock starts at the later of the last message and the job's `active_at` in `list_other`. Only an agent the seat started is stopped, never an adopted one. A failed stop retries at the next tick. No notice goes out.
5. **empty** after an idle stop, message: `resume_other` the stopped job with the waiting lines as its prompt, so the agent keeps its conversation, then apply the seat's switch to it. → starting. A refused resume falls back to step 1 with the previous job's transcript in the brief and no `replaces`. A resume that does not come up is not tried again.
6. An agent the seat started that ends, or stops taking pushes, gets a notice and no restart. Its undelivered rows start the next agent. An adopted session ends with no notice.
7. **After a restart**: undelivered rows go to a live agent, or cold-start. Slack resync routes only messages after `synced_ts` that are not in `seat_routed`. Discord gets a per-chat cursor. Nothing older starts an agent.

**Trust.**
- `forward` joins `SEAT_CHAT_VERBS` (`core/crates/awm-trust/src/trust.rs:122`).
- `_mount_refusal` (`hub_adapter.py:623`) carries over for `mount` and `seat_adopt`, so `force` never takes a trusted seat.
- An adopted job's level must be at least its seat's level.

**Edges.**
- A hand mount on a declared seat is refused. It names `seats adopt` on the seat's host, the one way to hand a session a declared seat.
- On the first Rust start, every live `social_mounts` row (20 today) becomes an adopted seat, keeper-started or not.
- Hand mounts on chats that no rule declares keep routing to their holder.
- The `social_quiet_threads` gate (`send quiet=`) carries over.
- The 60 s mount prune loop carries over.
- `paused` records each message to an empty seat as routed, so unpause replays nothing. An occupied seat, such as an adopted mount, still gets its lines. At unpause, social sends Tony's DM one line per seat chat that got messages while paused, with the chat and the count but no text, so no one waits unanswered without his knowing.
- An outside-lab sender (Slack restricted, or a foreign team) is held with a `blocked` card until `seat_admit`.
- A new DM files a `review` card and starts.
- A forward to an empty seat cold-starts it, except into a DM nobody opened, and grants a 24 h read of the source.
- "Cannot tell", including any `errors[]` for the node, never empties a seat.

**Line, brief, names.**
- The pushed line is `[<server>/<chat>/<sender>, <level>, msg <id>] ↳<thread> text`, cut at 1500 characters. The id lets `seats react` and `seats download` name a message other than the latest.
- A Discord line shows each mention by name, and the bot as `@you`. A channel message that mentions only others is stored and kept off the seat.
- The brief gives absolute paths under `<workspace>/projects/agentic-commons/main/docs/` (`seat-sop.md`, then `briefs/<platform>-<server-slug>.md`, or `discord-dm.md` for Tony), plus the seat facts.
- Sessions are named `seat:<platform>/<server>/<chat>`, or `seat:<platform>/dm/<person>` for a DM, with slugs in lowercase-hyphen form. Social derives the name from the stored row on each read and stores none. `social seats` gives it as `label`, which telemetry shows on Ops.

**Roster.** A seat lives while `social.toml` declares it. At startup social forgets each stored row the config no longer declares, re-sends each kept seat's declaration, then sends each host its roster. A host forgets any seat its roster lacks, and a live agent of that seat keeps running. `social seats` lists every stored seat, also one whose chat the platform has not listed yet, such as a Discord channel before the gateway is ready.
- **CAUTION** Social checks a stored row at startup with no sender, so a sender exception alone never drops a DM seat. Put the DM's channel id in the rule's `except` too, as for Slackbot.
- **CAUTION** A rollout restarts social before the seat hosts. A host on the older build acks a row kind it does not know and loses it. Restart social once more after a rollout that adds a row kind.
- **CAUTION** Telemetry reads `social seats` only when it connects. Restart telemetry after a change to the roster's shape.

Tests:
- one test per transition and edge;
- a no-drop property test under random reflection faults and restarts;
- a synthetic replay of Patrik Star's undelivered DM of 2026-11-07;
- resync dedupe and the send guard.

Gotchas: save the deadlines as timestamps so they survive a restart. Until knowledge's scope create hook lands, `ensure_seat` refuses a seat whose folder is missing and files `request_human`, and the message stays queued.

### Remote Control switch

Ops on the vault shows each seat with a Remote Control switch (`fleet.md` R147 item 7). The switch belongs to the seat, holds across empty spells and restarts, and starts off.

- **State.** Schema v10 adds `remote_control` and `rc_url` to `seat_registry`. Python social stops at v8 and never read the seat tables, so a rollback is unchanged.
- **Seat id.** `<account>/<channel_id>`, the table's key. The session name is the seat's `label`, because a chat rename changes it.
- **Verb.** `seat_remote_control {seat, on}` has no MCP tool. Only operations on this node may call it. On a seat whose agent was started or is up, it also calls reflection `remote_control` with the session name, each `/` written as `-` (`sessions.md` R146). The row then holds what the session really is, read back from `list_other`. A failed live call keeps the new switch for the seat's next start, and the seat event carries `rc_live: failed: <why>`.
- **Start.** `start_other` passes the seat's switch. A resume after an idle stop applies it.
- **Persistence.** cch records each switch on the job's lineage record. A daemon relaunch or a resume of the session brings the recorded switch back (`sessions.md` R146).
- **Adoption.** Adoption leaves the live session alone. An occupied seat records its switch as on, because every seat session before the switch started with Remote Control on.
- **Link.** `remote_control` and `rc_url` of an occupied seat follow the job's `list_other` row on every tick, so a switch made outside the seat shows on Ops.
- **Event.** `social/seats` carries `{seat, label, state, remote_control, node, model, effort, refresh, job?, rc_url?, ts}`. The seat's save emits it when one of those fields changed. A switch answer adds `rc_live` (`applied`, `none` or `failed: <why>`) and a Refresh answer adds `refresh_live` (`pending` or `kept`). Either answer always emits, so Ops can settle a pending request. At boot each seats plugin reports every seat, and social emits only for a seat whose evented fields changed. A subscriber reads `social seats` once when it connects.

### Model, effort and Refresh

Ops shows each seat's model and effort beside its switch, with a Refresh button that applies a new pick.

- **Pick.** The pick lives on the seats plugin's row, not on the decl, so a `social.toml` change keeps it. The seat runs on the pick, else the decl's pair, else the node's settings. `model` and `effort` in the event are that resolved pair.
- **Verb.** `seat_refresh {seat, model, effort}` has no MCP tool. Only operations on this node may call it. It refuses an effort above high. It does not check the model name, because cch takes any model id.
- **Refresh.** On a seat whose agent the seat started, the refresh stops the agent at the next tick it is not mid-turn, then resumes its conversation on the pick. An empty seat keeps the pick for its next start (`kept`). An adopted seat keeps the pick and is never stopped, because the seat cannot start that session again.
- **Resume.** The resume takes the pick through `resume_other`'s `model` and `effort`.
- **Restart line.** The resumed agent gets a `[seat]` line that names its new pair. That line has no message id, so it gets no 👀. The agent's reply target falls back to its predecessor's last line.

**CAUTION** No session event marks the end of a turn. While a refresh waits on a busy agent, the seat looks at it on every tick (15 s).

**CAUTION** cch applies a resume's `model` and `effort` to a retired session only. The seat's stop removes the job, which makes it retired. A stop that kept the job would resume the agent on its old pair.

**CAUTION** A start emits `starting` twice: once before reflection answers, and once with the job id.

### Land

It lands after awm-store, the alcor drop-in, reflection's `probe`, `gone` and transcript path, and after ops' first land, which carries awm-statecheck. Ansible owns alcor's `social.toml` (`mira_copy` with `social_for_alcor`, and `discord_seats` rewriting `node =` lines). A cloud change in the same batch therefore does three things:
- emits `[seats]` with `paused = true`, plus `[[seats.rule]]` and `[[seats.chat]]`;
- backs up the file;
- retires `discord_seats`.

Without that change, a role rerun reverts the seats. Stop the Python seats before the Rust start. If they are live again, `seat_adopt` each job that is mid-task, and retire the idle ones first.

Check:
1. `social seats` and `social accounts`, with the 20 mounts adopted.
2. Tony's live DM test through his adopted seat: it reaches exactly one job, and the reply comes back.
3. `awm-statecheck check`, gated on `social_history` row counts per chat, which must not drop.
4. Only social restarted.
5. Each occupied adopted row shows `remote_control: true`, and its session is untouched. Each empty row shows `false`.
6. A `seat_remote_control` from the CLI is refused. The DM test of step 2 shows the seat's events on `awm services listen social seats`.

From the cutover on, down notices go to Tony's Discord DM. #notifications was only the Python code's default.

Gotchas: confirm that `awm-plugin@social` runs the new binary. Grep mizar's release for `awm.kept` importers before deleting it.

### Turn the seats on

When Tony says so, flip `[seats] paused = false` through the cloud role. Check that Tony's DM gets the paused-window summary: one line per seat chat with its message count. Then send one test DM to an empty lab seat. Check one 👀, one start on altair, delivery once, and no backlog replay.

Gotchas: `paused` recorded every message as routed, so the unpause starts nothing by itself.

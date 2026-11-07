# Port plan: social

## Purpose & Contents

This file holds the approved port plan for social and its seats component, from the comms group's plan (task CWPYAtyb), approved by Tony on 2026-11-06. It names only what the Python does not make obvious. The Python at release 49232bf9 is the reference for everything kept. The rules every comms port follows are in `comms.md`.

## Plan

### Accounts, connectors, buckets and verbs

This lands together with the seats component.

Replaces `awm/services/social/` (11,283 lines). It runs on alcor as `awm-plugin@social` with `AWM_DIR=/var/lib/awm/social`.

Surface changes, listed:
- Removed `keepers`, `keep_reset` and `keep_adopt`, and the seven OneDrive-only bucket verbs.
- Added `mark_seat_done`, `forward`, `seat_reset`, `seat_adopt` (on CLI and HTTP) and `seat_admit`.
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

`social.db` v8 opens in place. v9 adds tables only. The keeper tables stay, unread.

social needs `reflection`. It calls `reflection` (`start_other`, `message`, `list_other`, `probe`), `operations request_human` and `journal tag_add`.

Tests: port the non-seat suites against loopback stand-ins, with Gmail attachment fixtures from Python's parser.

Gotchas:
- Slack Socket Mode acks within 3 s.
- The twilight DM-only command payload must equal discord.py's.
- Keep SQLite off the async loop.

### Seats

One module owns every seat in `social.db`: `seat_registry`, a durable `seat_queue`, `seat_routed` for dedupe, and `seat_lines` for the send/react defaults. Each seat's transitions run in one task per seat, and each transition writes the database before it acts.

**Config.** `[seats]` holds `project = "agentic-commons"`, opus and high, `paused`, and `notice`, which defaults to Tony's Discord DM. One `[[seats.rule]]` makes every slack-bot DM a cooperative seat on altair at `seats/{slug}`, except Slackbot. `[[seats.chat]]` overrides name Tony's two DMs (trusted; altair and mizar) and Hallam Hive #general (cooperative, altair).

**Transitions.**
1. **empty**, message: queue it, react 👀, run `ensure_seat` (`scopes create` on the seat's node), then `start_other {project, scope: "seats/<slug>"}` with `peer`. → starting. The rows go into the brief but stay undelivered.
2. **starting**: the agent's first live `list_other` row or social call marks the briefed rows delivered. → occupied. If the agent is not up within 3 min, requeue the rows and count one breaker attempt: 4 per hour, backoff 60, 120, 300 and 600 s, then a 6 h hold and a down notice.
3. **occupied**: each message is pushed. A Gone or exhausted push returns its rows → empty, then step 1.
4. `mark_seat_done`, or 4 h with no message: send the retire line → retiring.
5. **retiring**: reply once "agent is changing shift, will respond within 5 mins" and queue. When the agent is gone → empty. After 4 min, start the next agent beside it with the previous job's transcript path, without `replaces`, and unbind the old agent.
6. An agent that dies before done gets a down notice and no restart. Its undelivered rows start the next agent.
7. **After a restart**: undelivered rows go to a live agent, or cold-start. Slack resync routes only messages after `synced_ts` that are not in `seat_routed`. Discord gets a per-chat cursor. Nothing older starts an agent.

**Trust.**
- `mark_seat_done` and `forward` join `SEAT_CHAT_VERBS` (`core/crates/awm-trust/src/trust.rs:122`).
- `_mount_refusal` (`hub_adapter.py:623`) carries over for `mount` and `seat_adopt`, so `force` never takes a trusted seat.
- An adopted job's level must be at least its seat's level.

**Edges.**
- A hand mount on a declared seat adopts it, as with Kalen and Patrik.
- On the first Rust start, every live `social_mounts` row (20 today) becomes an adopted seat, keeper-started or not.
- Hand mounts on chats that no rule declares keep routing to their holder.
- The `social_quiet_threads` gate (`send quiet=`) carries over.
- The 60 s mount prune loop carries over.
- `paused` records messages as routed, so unpause replays nothing. At unpause, social sends Tony's DM one line per seat chat that got messages while paused, with the chat and the count but no text, so no one waits unanswered without his knowing.
- An outside-lab sender (Slack restricted, or a foreign team) is held with a `blocked` card until `seat_admit`.
- A new DM files a `review` card and starts.
- A forward to an empty seat cold-starts it, except into a DM nobody opened, and grants a 24 h read of the source.
- "Cannot tell", including any `errors[]` for the node, never empties a seat.

**Line, brief, names.**
- The pushed line is `[<server>/<chat>/<sender>, <level>] ↳<thread> text`, cut at 1500 characters.
- The brief gives absolute paths under `<workspace>/projects/agentic-commons/main/docs/` (`seat-sop.md`, then `briefs/<platform>-<server-slug>.md`, or `discord-dm.md` for Tony), plus the seat facts.
- Sessions are named `seat:<platform>/<server>/<chat>`, with slugs in lowercase-hyphen form.

Tests:
- one test per transition and edge;
- a no-drop property test under random reflection faults and restarts;
- a synthetic replay of Patrik Star's undelivered DM of 2026-11-07;
- resync dedupe and the send guard.

Gotchas: save the deadlines as timestamps so they survive a restart. Until knowledge's scope create hook lands, `ensure_seat` refuses a seat whose folder is missing and files `request_human`, and the message stays queued.

### Land

It lands after awm-store, the alcor drop-in, reflection's `probe`, `gone` and transcript path. Ansible owns alcor's `social.toml` (`mira_copy` with `social_for_alcor`, and `discord_seats` rewriting `node =` lines). A cloud change in the same batch therefore does three things:
- emits `[seats]` with `paused = true`, plus `[[seats.rule]]` and `[[seats.chat]]`;
- backs up the file;
- retires `discord_seats`.

Without that change, a role rerun reverts the seats. Stop the Python seats before the Rust start. If they are live again, `seat_adopt` each job that is mid-task, and retire the idle ones first.

Check:
1. `social seats` and `social accounts`, with the 20 mounts adopted.
2. Tony's live DM test through his adopted seat: it reaches exactly one job, and the reply comes back.
3. `awm-statecheck check`, gated on `social_history` row counts per chat, which must not drop.
4. Only social restarted.

From the cutover on, down notices go to Tony's Discord DM. #notifications was only the Python code's default.

Gotchas: confirm that `awm-plugin@social` runs the new binary. Grep mizar's release for `awm.kept` importers before deleting it.

### Turn the seats on

When Tony says so, flip `[seats] paused = false` through the cloud role. Check that Tony's DM gets the paused-window summary: one line per seat chat with its message count. Then send one test DM to an empty lab seat. Check one 👀, one start on altair, delivery once, and no backlog replay.

Gotchas: `paused` recorded every message as routed, so the unpause starts nothing by itself.

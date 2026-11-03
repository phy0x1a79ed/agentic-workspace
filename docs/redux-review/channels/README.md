# channels

## Purpose & Contents

This doc holds the review of channels: how they are built today, where the build and `docs/redux/channels.md` disagree, and each decision Tony makes on them. The settled decisions go into `channels.md` as requirements.

## As built

Surveyed read only on mizar, 2026-11-03.

- **origin/release at 266f4b29** has none of R71 to R73, R76, R77, R110, R111, R125, R127 or R138.
  - Seats are the `social` service. Each message is pushed into the holder through `reflection message`, capped at 1500 characters, with no trust tag (`social/seats.py:28`, `seats.py:83-99`). The Slack connector still runs Tony's own `xoxc` session and its `send` (`social/connectors/slack_conn.py:95`, `:360`).
  - The tether is its own tool. Events arrive by `drain` under a Monitor (`tether/hub_adapter.py:473`, `:818`). The first code word names the slot (`hub_adapter.py:569`). The PowerShell launcher is declared in `tether/remote_os.py:22`.
  - The board door and intake stay live. The edge passes board paths with no session (`httpsfront/board.py`). `reporting` serves intake (`reporting/hub_adapter.py:452`).
  - Peers keep the domestic and foreign trust record (`gateway/peers.py:10`).
- **The lane `feat/redux-channels`** (bbc0b926, worktree `redux-channels`) holds 6 commits on 6d83b353, 45 behind `feat/redux`. Plan: `docs/redux/drafts/channels-plan.md`. Its 36 plugin tests pass against a real relay binary, and the edge and caller tests pass (71 and 56). They need `spake2`, which no env on mizar holds. The Rust and PowerShell tests need containers and did not run.
  - The `channel` plugin (`awm/plugins/channel/`) serves `open` with kind room or machine, `write`, `status`, `close` and `encrypt`. Only a background job may hold a channel (`holders.py:60`). Every channel is untrusted, fixed in code (`service.py:29`, `:63`, `:257`).
  - A pushed line is `<kind> <name> [untrusted] from <who>: <text>`, cut at 1500 characters, and names a transcript file with the full text (`lines.py:9-24`). A prune loop closes a channel once its holder's job is gone (`service.py:153-165`).
  - `encrypt` runs symmetric SPAKE2 over Ed25519 in the live room, then HKDF-SHA256 and ChaCha20-Poly1305 per direction (`crypto.py`). After it our side refuses plaintext.
  - The machine kind calls the `tether` tool's `invite`, `send`, `drain` and `cut` through the gateway (`machines.py:36-57`). Owner input is pushed. Command output stays on `drain`. The shell verbs `run`, `shell` and `keys` stay on `tether`.
  - The edge forwards `/tether/r/<name>` and its `send`, `recv`, `ws` and `leave` routes (`httpsfront/tether.py:79-96`). The plugin's room link is `https://nexus.tony-xy-liu.com/tether/r/<name>` (`relay.py:12`).
  - The plugin loads only through `feat/redux-core`'s plugin discovery (`gateway/hub/discovery.py`). Today's gateway does not scan `awm/plugins`.
- **The tether lane `feat/redux-channels`** in `projects/tether/redux-channels` holds 4 commits on its local `release`, unpushed.
  - 2d34bcc wraps `launcher.ps1` in one script block, so a tether end returns the PowerShell prompt (R138).
  - 3268def draws a random free slot (`tether-relay/src/session.rs:245`, R76).
  - 47bdc3c and a046742 add rooms to the relay: `POST /room` with the operator bearer, then `/r/<name>` routes (`tether-relay/src/lib.rs:160-168`). A name is three words from the tether list, about 30 bits. Rooms live in memory, at most 64, 64 KiB a message. A room ends after 10 minutes of host silence or 24 hours in all (`config.rs:154-157`). The how-to states the escalation and the soft key pinning of R73 item 5.

## Differences from the design

- **R72 item 5 (persistent rooms).** No room outlives its task. The relay ends every room after 24 hours, and the plugin closes it when its holder's job ends. R77 says a room exists only while a task holds it open, and deneb holds no always-on door. R72 item 5 asks for a room that outlives any one task.
- **R72 item 1.** The link is `/tether/r/<name>`, not `/r/<name>`.
- **R72 numbering.** R72 has two items numbered 5.
- **R110.** Rooms and tethers are not one tool. `tether` stays for the shell verbs, and `channel` drives it. `install.md` R115 lists `tether` as a core plugin and does not list `channel`.
- **R69.** The channel push loop copies `social`'s `Pusher` and does not share it. Seats send with `social send`, channels with `channel write`.
- **R77 and R145.** R77's bullet names the reporting front door, and R145 removes `reporting` and `board` entirely. R72 item 5 lets the board door and intake go only after federation threads move onto persistent rooms. The queue cutover plans to remove `board` with `reporting`. Caviar's listener and the muse link still run on the board.
- **R111 and R125.** The plugin admits any background session. It checks no trust level, and nothing says whether a cooperative seat may open a room or a machine channel.
- **R76, R138.** Fixed only on the unpushed tether lane. awm pins a tether release that has neither fix.
- **R71.** Seat lines on release carry no trust tag. `feat/redux-trust` adds one in `social/seats.py`. Channel lines carry one on this lane.
- **R127.** Release still sends on Tony's Slack session.
- **Lane gaps.** A machine channel held from another node records no tether operator, so a local session that names the slot can adopt it. Every websocket guess at a room name spends one shared miss budget, because the edge forwards no client address on an upgrade. A tether journal epoch change resets the drain cursor and nothing handles it.

## Decisions

- 2026-11-03: Settled by existing requirements, without a question to Tony.
  - `install.md` R115 item 4 lists `channel` in place of `tether` as a core plugin, because R110 makes rooms and tethers one tool.
  - `queue.md` R145 removes the board only after the federation threads move onto persistent rooms, as R72 item 5 orders.
  - A cooperative seat may open rooms and machine channels. R125 allows every tool its edges and trust level allow, and its exceptions do not name channels.
  - R72's second item 5 is now item 6.
- 2026-11-03: A persistent room has no time cap and ends with its session. Its name is six words, and a room for one task keeps three. One session may hold several rooms, timed and persistent together, and may close any early. R77 says a session holds a room, not a task (`channels.md` R72 item 5, R77). Tony: "agree on all 3 for persistent rooms" and "a session can dispose a room early right? and a session can hold multiple rooms? inlcuding some that are timed and somet aht are persistent?".

## Follow-ups for the build

- Rebase the lane on `feat/redux`.
- Push and release the tether lane (R76, R138), then bump awm's tether pin.
- Share one push loop between `social` and `channel` (R69).
- Record the tether operator for a machine channel held from another node, so a local session cannot adopt the slot.
- Make the edge forward the client address on a websocket upgrade, so each guesser spends its own miss budget.
- Handle a reset of the tether's journal epoch in the drain cursor.
- Add `spake2` to the plugin's install.
- Add a persistent kind to the relay's rooms and to `channel open`: no 24-hour or idle expiry, a six-word name, and a close when the holding session ends.
- Keep Caviar's listener and the muse link on the board until persistent rooms carry them.

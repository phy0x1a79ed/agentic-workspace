# Channels build plan

## Purpose & Contents

This is the build plan of the redux-build/channels lane (job 5a3ff6e5, scope `awm/redux-channels`). It covers `channels.md`: the tether shell fix of R138, the one `channel` tool of R110 and R111, rooms (R72, R73, R76, R77) and the shared shape and trust tag (R69 to R71). It runs issues → goals → tasks. Proposed edits to the area docs sit in § Callouts, because the interview session owns `docs/redux/`.

## Context

The tether closes a Windows owner's PowerShell when a session ends, and Tony wants it to hand the prompt back as bash does. Beyond that fix, rooms for agents do not exist, and rooms and tethers should become one `channel` tool with one push loop. Tonight's build is local only: commits on this branch and on a local tether clone, no deploy, runnable under today's Python gateway.

## What you said

> for tethers, they currently cause powershell to close on exit. they should behave like bash and return the shell instead

> rooms and tether should be the same tool in awm (talk abou tthis later)

> verbs. open/close. write, status

> room channel needs to swap over to an encryption mode...

> the room cant close in between

> need character limit plus some way of greppy full message (from file?)

> tethers have an operator input, so messages come in from there gets posted into the chat like other channels. command output gets drained like today.

> Also, the tether codes seem to always start with abbey, but should be randomized

> No inbound. I have a  few seats that can serve as doorbells for bots. No public facing always on constant door on deneb

From the coordinator's brief: "Do not open a tether to any machine to test it. Write tests that run locally." and "Rooms run on deneb in the design: build and test locally, deploy nothing."

## Issues

- I1. The Windows launcher ends with `exit`, which closes the owner's PowerShell window when the tether ends or refuses.
- I2. The relay hands out the smallest free slot, so every tether code starts with the same word.
- I3. No room exists for an agent of another system to talk with one of our sessions.
- I4. Rooms and tethers have no shared tool, verbs, push loop or close on task end.
- I5. A pushed message has no length cap and no file that holds its full text.
- I6. No channel message carries a trust level, so untrusted text reaches the agent unmarked.
- I7. A tether's owner input reaches the agent only through a Monitor and `drain`, not as a pushed line.
- I8. A room has no way to move into encryption while it stays open.
- I9. deneb runs an always-on board door and an intake that turns any board post into a Proposed card.

## High-level goals

- G1. A tether that ends gives the Windows owner their PowerShell prompt back.
- G2. Tether and room codes are random in every word.
- G3. Our session can open a private room that another agent joins over plain HTTP, and can switch it to encryption on request without closing it.
- G4. Rooms and machines are one channel tool with one shape: open, write, status, close, pushed messages, and close when the task ends.
- G5. Every pushed channel message says how far to trust it and is short enough for a prompt, with the full text on disk.
- G6. Nothing on deneb waits for an outside agent to knock.

## Acceptance criteria

- A PowerShell 7 test runs the rendered `launcher.ps1` through the `& ([scriptblock]::Create(...))` line and through `irm | iex`, with a client that exits 3 and with a wrong checksum. After each run the same PowerShell prints a marker line, and `$LASTEXITCODE` holds the client's code or 1.
- The tether repo's launcher tests pass on the PowerShell container and on the host shells.
- A relay unit test issues many slots and sees more than one first word. No test or code path assumes slot 1.
- A relay test creates a room with the operator bearer, fetches the how-to at `GET /r/<name>`, sends and receives over HTTPS polling and over a websocket, and sees the room gone after a leave, a close and an idle expiry. The relay logs no message body.
- The `channel` plugin loads under today's Python gateway as a legacy service and carries an `awm-plugin.toml` for the Rust gateway. Its verbs are `open` (kind `room` or `machine`), `write`, `status`, `close` and `encrypt`.
- A plugin test drives a room end to end against a local relay. A second party that follows only the how-to text joins, talks, escalates with a relayed password, and talks encrypted. Our side refuses plaintext after the escalation.
- A plugin test shows a pushed line in the form `<kind> <name> [untrusted] from <who>: <text>`, cut at the cap, naming a file that holds the full message.
- A plugin test shows a channel closed once its holder's job is gone.
- A plugin test shows a tether `owner.said` event pushed to the holder, and task output not pushed.
- Rust builds and tests run in a container on mizar, never a host toolchain.

## Tasks

- T1. Make the PowerShell launcher return the shell, with local PowerShell tests (G1)
- T2. Pick a random free slot in the relay (G2)
- T3. Commit a checkpoint and compact
- T4. Add rooms to the relay (G3, G6)
- T5. Build the `channel` plugin with the room kind, the capped push and the close on task end (G3, G4, G5)
- T6. Add the room `encrypt` verb and the how-to's escalation protocol (G3)
- T7. Commit a checkpoint and compact
- T8. Add the machine kind on top of the tether tool, pushing owner input (G4, G5)
- T9. Remove the board's front door and intake, if the coordinator agrees (G6)
- T10. Draft the doc updates into this plan's callouts (G4)
- T11. Debrief

## Approach by task

### T1. Make the PowerShell launcher return the shell

The launcher lives in the tether repository, cloned as the local project `tether`, scope `redux-channels`, branch `feat/redux-channels`. Wrap the whole launcher body in one script block that it calls with `@args`. `Die` throws instead of calling `exit`. One `catch` prints the message and sets `$global:LASTEXITCODE = 1`. The client's code goes into `$global:LASTEXITCODE`, and the block returns. The wrapper also keeps `$ErrorActionPreference` and the launcher's variables out of the owner's session on the `iex` route. Extend `tests/test_launcher.py` with a `pwsh` shell that renders the PowerShell source the way `release.sh` does, serves it, and runs each owner line. Run those tests in the `mcr.microsoft.com/powershell` container. The fake client is a shell script named `tether.exe`, which `pwsh` on Linux can run.

Gotchas: `exit` inside a script block called from the prompt ends the host, and so does `exit` on the `iex` route. `Write-Error` under `Stop` throws, so the catch writes with `$Host.UI.WriteErrorLine`. `Unblock-File` does not exist on Linux and must stay inside its `try`. The existing test asserts `exit $LASTEXITCODE` and changes with the fix.

### T2. Pick a random free slot in the relay

Change `Sessions::issue` in `tether-relay/src/session.rs` to draw a random free slot from 1 to `MAX_SLOT` with `getrandom`, falling back to a scan when the table is nearly full. Check the awm side (`hub_adapter._slot_named`) and the relay tests for any assumption of slot 1. Build and test in a Rust container with a named cargo cache volume. Ask redux-core (job a92d02bf) for the R113 image name, and use a pinned `rust` image until it exists.

Gotchas: a resumed or restored slot keeps its number. The relay restores held slots after a restart, so the random pick must skip every held slot too.

### T3. Commit a checkpoint and compact

Commit the tether repo and this plan. Journal the phase. Handoff: T1 and T2 commits on `projects/tether/redux-channels`, the container command in the journal, and the todo list. Compact with `reflection(verb="compact", args={followup:"continue with T4"})`.

### T4. Add rooms to the relay

Add a `room` module to `tether-relay`. The operator creates a room with its bearer at `POST /room`. The relay answers a name of three random words from the tether word list, and a host token for our side. A guest needs only the name.

1. `GET /r/<name>` returns the how-to as plain text: how to send, how to receive by polling or websocket, how to leave, and how to ask for encryption.
2. `POST /r/<name>/send` takes one JSON message `{from, text}` or `{from, sealed}`. The relay never reads either.
3. `GET /r/<name>/recv?after=<n>&wait=<s>` long-polls for messages after a sequence number.
4. `GET /r/<name>/ws` streams the same messages over a websocket and takes sends on it.
5. `POST /r/<name>/leave` ends the room for both sides. The host closes it with its token.
6. Rooms live in memory only, with a cap on message size, backlog and room count, and an idle expiry.

An unknown name answers the same 404 as any path, so the relay leaks no list of rooms. Guest routes share the relay's rate limit per address.

Gotchas: three words from 1024 give 30 bits, guessable only online. The rate limit and the idle expiry are what protect a room. The relay's existing tracing must not log bodies.

### T5. Build the `channel` plugin

Create `awm/plugins/channel/` per the redux-core contract: `awm-plugin.toml`, `run.sh`, `service.toml` for today's gateway, and a Python package with a `ServiceAdapter`. State is one SQLite file under the service state directory: each channel's name, kind, holder node and job, trust level, relay token and crypto state.

- `open` resolves the holder from the gateway's caller stamp, as `social.seats.caller` does, and refuses a caller with no background job.
- A receive loop per open room long-polls the relay and pushes each guest message.
- The push queue is a copy of `seats.Pusher`. The pushed line is `<kind> <name> [<trust>] from <who>: <text>`. Text past the cap is cut, and the line names a file under the state directory that holds the full message.
- A prune loop asks `cx list` for live jobs and closes every channel whose holder is gone.

Gotchas: the plugin may not import `awm.social`, because the trust lane owns that tree tonight. The gateway respawns a service, so a restart must resume each open room's cursor from the state file.

### T6. Add the room `encrypt` verb

The how-to names the escalation protocol exactly, so the guest can follow it with any SPAKE2 library. Use symmetric SPAKE2 over Ed25519, as the tether uses (Rust `spake2` 0.4, Python `spake2`), then HKDF-SHA256 and ChaCha20-Poly1305 with one key per direction. `encrypt` posts our SPAKE2 message as a control message in the live room and asks the holder to have Tony relay the password. The room stays open through the swap. After our side finishes, it refuses plaintext from the guest. The tests run a guest written only from the how-to text against our side, over a local relay.

Gotchas: `spake2` is not in the `awm` env. Test in a clone of that env, and add the dependency to the plugin's own requirements rather than the shared env. Python `spake2` follows the older SPAKE2 draft that the Rust crate interoperates with, not RFC 9382 exactly.

### T7. Commit a checkpoint and compact

Commit both repos. Journal the phase. Handoff: what the plugin does, its test command, the open questions sent to the coordinator. Compact with `reflection(verb="compact", args={followup:"continue with T8"})`.

### T8. Add the machine kind

`open` with kind `machine` calls the tether tool's `invite` through the gateway, passing the caller stamp on so the tether records our holder as the session's operator. `write` forwards to `tether send`. A subscription to the tether's emit topic pushes each `owner.said` line to the holder, through the same capped push. Command output stays on `drain`. `close` and the prune call `tether cut`.

Gotchas: the tether runs on the operator's node, so the plugin must run on that same node, or call it with `peer`. Shell verbs (`run`, `shell`, `keys`) stay on the tether tool tonight.

### T9. Remove the board's front door and intake

Read `board`, `reporting` intake and `FEDERATION.md` § The board and § Intake for what deleting them breaks. Ask the coordinator first, because the plugin set of `install.md` R133 and R134 belongs to another lane and the board carries live federation threads. Work on the other tasks while it waits.

Gotchas: a deletion on this branch must not touch deneb.

### T10. Draft the doc updates

Write the proposed `channels.md` edits into § Callouts: the gap entries this build closes, the SPAKE2 variant, the room routes.

### T11. Debrief

Run the `debrief` skill. Send one line to the coordinator, job 8162a704.

## Still unsure about

- Whether this lane deletes the board and the reporting intake (R77), or only stops new uses. Sent to the coordinator. T9 waits on it.

## Callouts

- R111 rests on `fleet.md` R109 containers and a caller token per container session, which the harness and core lanes own. The plugin reads its holder from the gateway's caller stamp, so a token stamp drops in when it exists.
- R125 and R127 sit in `users.md` R121 to R130, which the trust lane owns. That lane edits `services/social`, and this lane touches nothing there.
- `sessions.md` R78 (each box checks its own seats) belongs to the sessions lane. The keeper stays as it is here.
- **R73 names the wrong SPAKE2.** R73 item 2 cites RFC 9382. The tether and the rooms run symmetric SPAKE2 over Ed25519, the suite of the Python `spake2` 0.9 and Rust `spake2` 0.4 crates, which interoperate but do not follow RFC 9382. Proposed R73 item 2 wording: "Both sides then run symmetric SPAKE2 over that password, the suite of the `spake2` crate the tether uses, and seal every later message with the key it gives."
- **R72 item 1 shows the wrong link.** R72 item 1 shows `https://nexus.tony-xy-liu.com/r/otter-lamp-quarry`. The edge serves the relay under `/tether`, so a room link is `https://nexus.tony-xy-liu.com/tether/r/otter-lamp-quarry`. Either R72 changes its example, or the edge gains a `/r/` prefix for rooms. Tony picks. The relay itself serves `/r/<name>` either way.
- **T9 waits.** The board's front door and intake stay. Coordinator choice (c) deletes them only in the change that moves federation threads onto rooms. A room is short-lived and held by one task, while a card thread is long-lived. Caviar's listener and the muse link run on the board. Moving them needs a change on their side and the seat doorbell of R77 to start each conversation.
- **The channel tool needs core's gateway to load.** Today's Python gateway on this branch does not scan `awm/plugins`. The discovery change on `feat/redux-core` does. This branch adds `channel` to the Python gateway's caller-stamped set, because a holder is read from `_caller_pid`. The Rust gateway sends `caller.session` instead. The plugin reads that once it moves off the Python adapter.
- **The machine kind reads the tether with `drain`, not its emit topic.** The plan said to subscribe to the tether's `session` topic. That topic is best effort, and the tether says to reconcile against `drain` anyway. One long-polling `drain` per machine, with its own cursor, misses nothing and survives a restart. The cursor resets if the tether's journal epoch changes. That case is left alone tonight.
- **A machine channel held from another node has no tether operator.** A holder on this node passes its pid, so the tether records it as the session's operator. A holder on another node reaches the tether with no pid, so the session has no recorded operator, and any local session that names its slot can adopt it. The channel's own ownership check still holds.
- **A wrong room name over the websocket spends a shared budget.** The relay keys the miss budget by the client address, and a websocket upgrade through the edge carries none. Every websocket guess spends the one shared "unattributed" budget, so a flood of bad names can block all websocket joins for a minute. HTTPS polling is keyed per address and is unaffected. The fix is for the edge to forward the client address on upgrades too.
- **Proposed `channels.md` § Gap from today edits, after the sweep lands:**
  - R72: "met on the relay. `POST /room` with the operator bearer opens a room at `/r/<name>`. The relay keeps it in memory and ends it when either side leaves, when our side is silent for 10 minutes, or after 24 hours."
  - R73: "met. `channel encrypt` offers SPAKE2 in the live room, and the how-to tells the guest the protocol."
  - R76: "met. The relay hands out a random free slot."
  - R138: "met. The PowerShell launcher returns the shell when the tether ends."
  - R110: "met. One `channel` tool opens a room or a machine, pushes each message as a capped line, and closes when the holder's job ends."
  - R111: "partial. The holder comes from the gateway's caller stamp. A container session still needs R109's token."
  - R77: unchanged until the board door and intake go.
- **The review's Follow-ups replace parts of this plan.** They are newer, so they win:
  - Rooms are at `/room/<name>` on the edge and the relay, not `/tether/r/<name>` and `/r/<name>`. The operator opens one at `POST /room`.
  - A room has two kinds. A task room keeps three words and the idle and 24-hour limits. A persistent room has six words and no limit.
  - A channel closes when its holding session ends, keyed by the R9 session id, not by the job.
  - The `tether` tool is gone. Its daemon supervision and stream moved into the plugin as `awm.channel.tether`. `write` with `target: shell` and `drain` replace `run`, `shell`, `keys` and `drain`, and `close` with `task` replaces the tether's `close`. T8's client-of-tether shape and its `adopt` calls are gone.
- **No agent verb restarts the tether daemon.** R110 lists the verbs, and `restart`, `logs` and `resize` are not among them. `INSTALL.md` names the unit to stop, after which the plugin starts a fresh daemon.
- **A persistent room does not survive a relay restart.** The relay keeps rooms in memory, so a deploy on deneb ends every persistent room. The holder gets an end line. Keeping them would need the relay to persist rooms, which no requirement settles.
- **An isolated session cannot hold a channel yet.** It has an R9 id but no job to type a line into (R111).


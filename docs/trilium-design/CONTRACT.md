# Trilium design — contract for the ports

## Purpose & Contents

This file lists what the trilium-design build needs from each Rust port, so each port can land its part on its own. It holds one item per owner: port-ops, port-comms, port-harness, and the gateway. A last section names what this scope provides, so the ports know the names they read. The spec items in `docs/redux/` are the source of truth for behaviour. This file adds only names and shapes. A shape that a port plan already fixes keeps its name.

plugin-redux/interview hands each item to its owner. No item lands in `feat/trilium-design`.

## port-ops: operations and auth

Spec: `queue.md` R61-R66 and R137, `fleet.md` R84, R97 item 2 and R147 items 2 and 7.

C1 moves to this scope (plugin-redux/interview, 2026-11-07). The operations port does not port `writer.py`, `board.py`, `board/` or `board-install`. trilium owns them. The hidden verbs of the operations port plan stay with operations, with these changes:

1. `card_body_set` goes, because Proposed is gone.
2. `board_import` maps each Proposed card to History, with its body kept in `tasks.body`. It still imports every Ops note.
3. The sections are `needs_you`, `working`, `stale` and `history`. `history` replaces `done`, and its cursor label is `historyAt`.
4. `board_changes` carries, per card:
   - each session row with `node` and `rc_url`, so the board shows its claude.ai links;
   - `source_note`, the note id of a task started from a note;
   - the task's approvals, each with `request_id`, `state` (pending, approved, rejected, expired or cancelled), the request's summary, and its times.

5. operations emits on `operations/board` at each card event it appends: `{task_id, seq, ts}`, where `seq` is the event's `board_changes` mark. No event at boot. trilium on deneb subscribes at peer alcor and drains `board_changes` after its own mark on each message, so the writer needs no poll.

Stale (R61 item 4):
- operations on alcor subscribes to `telemetry/sessions`.
- A Working or Needs you card whose last live session goes moves to Stale.
- A live session that comes back moves its card to Working.
- Closing a stale card moves it to History.

The vault API, on alcor's address `auth.tony-xy-liu.com` (R64 item 5):
- Every route checks the device credential cookie and that `Origin` is one of `board_origins`. It answers CORS with that origin and `Access-Control-Allow-Credentials: true`. It refuses every other origin.
- An everyday action takes the device credential alone:
  - `POST /api/agent` with `{node, note_id?}` starts an agent. It opens a card in Needs you and issues an `agent` order to the node. It answers `{task_id, order_id}`. With `note_id`, the card's `source_note` is that note.
  - `POST /api/card/{task_id}/close` and `POST /api/card/{task_id}/reject`.
  - `POST /api/seat/remote_control` with `{seat, on}` calls social's `seat_remote_control`. The seat id `<account>/<channel_id>` holds a slash, so it goes in the body, not the path.
  - `POST /api/request/{request_id}/reject`.
- An approval takes the passkey, in the modal (below).

The `agent` order, which replaces `spawn` and `new_from`:
- The order's payload is `{task_id, node, note_id?}`. operations on alcor builds the start from it. No text from the vault reaches the agent.
- The executor on the node calls reflection `start_other` with `suggest: true`, which starts in the lobby of `scopes.md` R40, with Remote Control on and the task id.
- With no `note_id`, the start has no prompt.
- With a `note_id`, the prompt is one fixed line from operations: read the note `<note_id>` as context only, then wait for Tony.

The approval modal (R64 item 5, R137):
- `GET /modal/approve/{request_id}` shows exactly what the request asks, including any batch or window it names. It takes the passkey.
- `GET /modal/twofa?count=1|2|4` arms a 2fa burst with one passkey tap.
- The modal tells its parent the outcome with `postMessage({type: "awm-approval", request_id, ok})`. Its `frame-ancestors` is `board_origins`.

The sign-in (R84 items 1-2):
- `GET /signin?next=<url>` on alcor's public address. One passkey tap starts a 12-hour session. A live session succeeds with no tap.
- The sign-in sets the device credential as a `__Host-` cookie on alcor's address, HttpOnly, Secure and SameSite Lax.
- The page lists every surface. Each link carries a short `portal_login` grant.
- `GET /signin?frame=1` is the vault's frame. It tells its parent `postMessage({type: "awm-signed-in"})`. Its `frame-ancestors` is `board_origins`.
- auth on each node redirects a visit with no session to `/signin?next=<the visit>`, and accepts the grant on return.

Removed:
- the actions `note_new`, `spawn` and `new_from`;
- the pages `/panel` and `/panel/button`;
- the hash chain: `/api/act`, `/api/resume` and `approver/chain.py`, with the device chain state that only it reads;
- the signed-read headers `x-awm-device`, `x-awm-ts` and `x-awm-sig`, which the cookie replaces.

Cloud batch:
- Add `telemetry` to the `singletons` of `edges.yml`, hosted on alcor. Its `callers` are every node that opts in, stations included, and deneb, so each node part reports to alcor and trilium on deneb reads the roster.
- Every node but alcor adds `telemetry` to its install config `exclude`, as for social and operations.
- Every node that reports, alcor included, sets `[telemetry] swarm = "<name>"`. A node part with no swarm stays idle, and a roster with none refuses every report.

## port-comms: social

Spec: `fleet.md` R147 item 7, `sessions.md` R146.

1. Each seat in `seat_registry`, keyed by `<account>/<channel_id>`, gains `remote_control`, a bool that starts false. An adopted occupied seat starts true, matching its live session. It holds across empty spells and restarts. An empty seat can be switched.
2. A new hidden verb, `seat_remote_control {seat, on}`, sets it and answers `{ok, seat, remote_control, live: none|applied|failed: <reason>}`. Its caller gate admits only operations on the same node.
3. A switch on an occupied seat calls reflection `remote_control` for the seat's job. An order to a session in mid-turn waits for the turn to end.
4. `ensure_seat`'s `start_other` passes the seat's `remote_control`.
5. `seats` rows gain `remote_control` and `rc_url`.
6. social emits on `social/seats` at each seat's state change and switch change: `{seat, label, state: empty|starting|occupied|retiring, remote_control, node, job?, rc_url?, ts}`, with `ts` in RFC 3339 UTC. `label` is the `seat:<platform>/<server>/<chat>` name, so a chat rename shows as an event. No event at boot. A subscriber reads `social seats` once when it connects.

## port-harness: reflection

Spec: `fleet.md` R147 item 1, `queue.md` R63 item 1, `scopes.md` R40.

1. reflection emits on `reflection/sessions`, on each node:
   - `{event: "started" | "stopped" | "remote_control" | "renamed", job, session_id, name, node, project, scope, kind, task_id?, rc_url?, ts}`;
   - `stopped` for every end, a crash included, within seconds of the process exit;
   - no event at boot. A subscriber reads `list_other` once when it connects.
2. `list_other` rows gain `rc_url` and `task_id`, and each row carries `session_id`, so telemetry keys a session it first sees at connect by its id, not its job.
3. `remote_control` on a session in mid-turn applies when the turn ends.

## gateway: plugin-redux/interview

Spec: `fleet.md` R147 items 1 and 6.

`awm/plugins` already covers a plugin's install changes. It does not cover a running service. The gateway adds two topics:
1. `awm/services`: `{service, plugin, event: "up" | "down", pid, ts}` at each register and each disconnect. No event at boot. A subscriber reads `GET /hub/table` when it connects.
2. `awm/usage`: each usage event as the gateway appends it to `<AWM_DIR>/usage/`, the same object. On a singleton's host, a call that arrives through the door records its `tool`, so telemetry derives the singleton's last response.

Both stay local to the node, under the privacy rules of `drafts/plugin-contract.md` § Peer calls.

## What this scope provides

telemetry, as two core plugins on one library. The loader refuses a whole plugin that declares a singleton another node hosts, so the node part needs a manifest of its own:
- `core/plugins/telemetry-node/`, on each node that opts in, alcor included: a service with no tool. It follows the node's emit topics and calls the hidden verb `telemetry report` with no peer, so the gateway sends it to alcor's door.
- `core/plugins/telemetry/`, on alcor: the singleton tool `telemetry` with the verbs `nodes`, `services` and `seats`.
- On alcor, the topic `telemetry/roster`, a change notice `{rev, node, changed: [nodes|sessions|services|usage|seats], ts}`. A consumer reads the verbs for the data.
- On alcor, the topic `telemetry/sessions`, at each session that goes live or goes: `{event: "live" | "gone", session_id, job, task_id?, node, ts}`.

trilium, in `awm/services/trilium/`:
- the writer, the board page, the Ops note and the per-note bot button;
- `[[needs.tool]] operations` major 2, as the operations port plan says.

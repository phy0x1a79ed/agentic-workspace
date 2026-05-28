# probe wire protocol

Two layers:

1. **Signaling** — JSON over MQTT topics on the EMQX broker. Used only to
   set up the WebRTC peer connection (SDP exchange, ICE trickle).
2. **Data channel** — JSON frames over the WebRTC data channel. Carries
   all command execution: `Exec` from the operator, `Stdout`/`Stderr`/
   `Exit` back from the friend.

Once the data channel is open, the broker is no longer in the data path.

### MQTT-relay fallback (UDP-blocked friends)

Some environments block all outbound UDP (HPC nodes, restrictive corporate
egress). WebRTC cannot work in those: the data channel rides DTLS-over-UDP
even when TURN is in use, and the current webrtc-rs (0.11 / 0.13) does not
implement TURN-over-TCP relay gathering — `agent_gather.rs` explicitly
warns "Unable to handle URL" for any `?transport=tcp` URL.

When both ends are launched with `--mqtt-relay`, layer 2 is replaced: the
same Frame JSON is published on `probe/<name>/from-{friend,operator}/data`
topics on the EMQX broker. The protocol is otherwise identical (same Frame
enum, same id/exit semantics). Trade-off: the broker sees command output;
no P2P privacy. Use only where WebRTC is infeasible.

---

## Signaling (MQTT)

Every probe instance is identified by a **name** (a string chosen by the
user, e.g. `mybox`, `alice-laptop`, `test-7f3a`). Topics:

| Direction        | Topic                                  | Purpose                       |
|------------------|----------------------------------------|-------------------------------|
| friend publishes | `probe/<name>/from-friend/sdp`         | SDP answer                    |
| friend publishes | `probe/<name>/from-friend/ice`         | one ICE candidate per message |
| friend publishes | `probe/<name>/from-friend/bye`         | graceful teardown signal      |
| friend publishes | `probe/<name>/from-friend/data`        | data frame (mqtt-relay only)  |
| operator publishes | `probe/<name>/from-operator/turn`    | ICE server config (optional)  |
| operator publishes | `probe/<name>/from-operator/sdp`     | SDP offer                     |
| operator publishes | `probe/<name>/from-operator/ice`     | one ICE candidate per message |
| operator publishes | `probe/<name>/from-operator/bye`     | graceful teardown signal      |
| operator publishes | `probe/<name>/from-operator/data`    | data frame (mqtt-relay only)  |

All messages: **QoS 1, no retain**.

### Ordering contract (operator → friend)

The operator MUST publish in this order when starting a session:

1. `turn` (optional — omit for STUN-only)
2. `sdp` (offer)
3. `ice` (zero or more candidates, trickled)

Friend processes events in delivery order. If `turn` arrives before `sdp`,
its ice_servers replace the friend's STUN-only default before the peer
connection is built. If `turn` is omitted, the friend stays on its baked-in
STUN fallback (`stun:stun.l.google.com:19302`); strict-NAT friends may then
fail to establish the data channel.

### Payload schemas

```jsonc
// .../sdp
{"type": "offer" | "answer", "sdp": "v=0\r\no=- ..."}

// .../ice
{"candidate": "candidate:...", "sdpMid": "0", "sdpMLineIndex": 0}

// .../bye
{"reason": "user requested" | "channel closed" | "error: ..."}

// .../turn  (operator-only; one or more server entries)
{
  "iceServers": [
    {
      "urls": [
        "stun:stun.cloudflare.com:3478",
        "turn:turn.cloudflare.com:3478?transport=udp",
        "turn:turn.cloudflare.com:3478?transport=tcp",
        "turns:turn.cloudflare.com:5349?transport=tcp"
      ],
      "username": "g071...",
      "credential": "396c..."
    }
  ]
}
```

The `turn` payload is the standard WebRTC `RTCIceServer[]` shape (urls
+ optional username/credential per entry). The operator is responsible
for normalizing provider quirks — Cloudflare Realtime TURN, for
instance, returns `iceServers` as a single object that the operator
must wrap into a single-element array before publishing.

### Name collisions

If two friends use the same name simultaneously, the second one's
publish on `from-friend/sdp` overlaps the first's session. Detection:
the friend subscribes to its own outgoing topic for 1s on startup; any
received message means the name is in use → exit with
`probe: name '<name>' is in use elsewhere`.

This is best-effort. Two friends starting within ~1s of each other can
both proceed and confuse the operator. User-provided unique names are
the contract.

---

## Data channel (WebRTC)

JSON-encoded `Frame` per WebRTC message. Binary payloads are base64.
Each frame carries `id: u64` so the protocol generalizes to concurrent
execs (v1 uses sequential ids per session: 1, 2, 3, …).

### operator → friend

```jsonc
{"type":"exec",        "id":1, "cmd":"echo hello"}     // spawn `sh -c <cmd>`
{"type":"stdin",       "id":1, "data":"<base64>"}      // reserved; v1 ignores
{"type":"stdin_close", "id":1}                          // reserved; v1 ignores
{"type":"chat",                 "message":"hello"}      // human-readable chat from operator
{"type":"hello", "scope":"capella-shell-scopes/alice-2026-05-27",
                 "operator":"alice"}                      // session metadata for friend's TUI
```

### Hello frame

`Hello` SHOULD be the first frame the operator sends after the data
channel opens (operator → friend only). It carries human-readable
metadata about the session — the vagrant scope the probe has been
attached to on the operator side, and an optional operator display
name. The friend's TUI renders the scope name in its header until then
it shows `—`. Both fields are optional; an operator that doesn't have a
scope name yet MAY omit the frame entirely.

### friend → operator

```jsonc
{"type":"stdout", "id":1, "data":"<base64>"}                       // stream chunk
{"type":"stderr", "id":1, "data":"<base64>"}                       // stream chunk
{"type":"exit",   "id":1, "code":0,    "signal":null}              // normal exit
{"type":"exit",   "id":1, "code":null, "signal":15}                // killed by SIGTERM
{"type":"error",  "id":1, "message":"spawn failed: ENOENT"}        // spawn or io failure
{"type":"chat",                "message":"ok checking now"}         // human-readable chat from friend
```

### Chat frame

`Chat` frames carry human-readable messages between operator and friend. They
are not bound to any exec `id`. The friend prints `[operator] <message>` to
stderr when it receives one; the operator displays incoming chat messages via
the TUI or prints `[friend] <message>` to stderr in REPL mode.

### Ordering guarantees

- `Stdout`/`Stderr` are emitted in source order per stream. Cross-stream
  interleaving reflects the OS scheduling (`stdout` and `stderr` are
  separate pumps).
- Exactly one of `Exit` or `Error` is emitted per `Exec`; it is always
  the last frame for that `id`.
- Chunks are capped at 16 KiB (the executor reads in 16 KiB buffers).
  No fragmentation logic at the protocol level — the data channel is
  reliable + ordered.

### Reserved frames

`Stdin` and `StdinClose` are wired into the type system but not handled
in v1. They become live when interactive pty mode lands (sibling
`PtySpawn`/`PtyData`/`PtyResize`/`PtyExit` frames will land alongside).

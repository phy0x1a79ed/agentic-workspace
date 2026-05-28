# probe (binary)

Single static Rust binary that runs on a friend's machine and opens an
ephemeral pair-debug session against an awm operator over a WebRTC data
channel. Downloaded to `/tmp` and exec'd directly — never installed to
PATH.

## Architecture (this phase)

```
  EMQX (signaling: SDP + ICE)
       ▲          ▲
       │          │
  ┌────┴──┐   ┌───┴────┐
  │ probe │   │ probe  │
  │ (Rust)│   │_op.py  │
  └───┬───┘   └────┬───┘
      │            │
      └─ WebRTC ───┘
        (DataChannel,
        DTLS-encrypted,
        P2P shell I/O)
```

Signaling rides on a serverless EMQX broker
(`wss://s12a68ff.ala.us-east-1.emqxsl.com:8084/mqtt`). Once the data
channel opens, shell I/O is P2P — EMQX never sees command output.

If `CF_TURN_TOKEN_ID` + `CF_TURN_API_TOKEN` are configured (see
`tools/.env.probe.example`), the operator mints a short-lived
Cloudflare Realtime TURN credential at startup and ships it to the
friend over the `probe/<name>/from-operator/turn` topic before the
SDP offer. Without those creds, both sides fall back to STUN-only.

See [`PROTOCOL.md`](./PROTOCOL.md) for topic and frame schemas.

## Prerequisites

- Rust stable (`rust-toolchain.toml` pins). Install via:
  ```sh
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
  . "$HOME/.cargo/env"
  ```
- Python 3.10+ with `aiortc` + `aiomqtt`:
  ```sh
  pip install -r ../tools/requirements.txt
  ```
- EMQX credentials in `../tools/.env.probe` (copy `.env.probe.example`).

## Build

```sh
cargo build --release            # → target/release/probe
cargo test                       # 21 unit tests
```

## Run (friend side, standalone)

```sh
./target/release/probe \
    --name mybox \
    --mqtt-url wss://s12a68ff.ala.us-east-1.emqxsl.com:8084/mqtt \
    --mqtt-user probe \
    --mqtt-pass <password>
```

The consent prompt blocks until you type `y`. After consent, the binary
connects to EMQX and waits for an operator. Use `--no-consent` to skip
the prompt (tests only).

## Run (full install + operate UX)

In one terminal, start the host server (serves the launcher + binary):

```sh
cd ..
python3 tools/host_binary.py
# listening on http://0.0.0.0:12110/
```

On the friend's machine:

```sh
curl -fsSL http://<host>:12110/probe | sh -s mybox
# probe v0.0.1 — vagrant-shell pair-debug binary
# probe is about to expose this shell to an awm operator.
# Continue? [y/N] y
# probe v0.0.1 starting (name=mybox)
# code=mybox
```

On the operator's machine (after `pip install -r tools/requirements.txt`):

```sh
# one-shot — fresh channel per call (full SDP/ICE handshake each time;
# the friend exits after Bye)
python3 tools/probe_op.py mybox exec 'uname -a'

# interactive REPL — one persistent channel, multiple commands
python3 tools/probe_op.py mybox
# [connected to mybox] Ctrl-D to exit
# > echo hello
# hello
# > <Ctrl-D>

# Persistent daemon + shell-friendly `send` (recommended for repeat use):
# one terminal:
python3 tools/probe_op.py mybox daemon                 # holds the channel open
# any other shell — no broker creds needed, pure local IPC over a unix socket:
python3 tools/probe_op.py mybox send 'uname -a'        # streams stdout/stderr,
                                                       # exits with friend's code
python3 tools/probe_op.py mybox send 'cd /etc && ls'   # each send is one sh -c
python3 tools/probe_op.py mybox stop                   # graceful shutdown
```

The daemon listens on `${XDG_RUNTIME_DIR:-/tmp}/probe-<name>-<uid>.sock`
(mode 0600). `send` opens that socket, ships one Exec, and proxies
stdout/stderr/exit back. Multiple back-to-back `send` calls reuse one
SDP/ICE handshake on the wire — no friend respawn between commands.

## End-to-end tests

Run the whole local suite (cargo + four python e2e harnesses) in one
shot. Requires `tmux` on PATH and `tools/.env.probe` populated:

```sh
cd ..
tools/test_local.sh
# === stage 1: build + cargo test ===
# ...
# === stage 6: tools/test_repl_mode.py ===
# === all local tests passed ===
```

Or run individual stages:

| script | what it covers |
| --- | --- |
| `tools/test_e2e.py` | one-shot exec over WebRTC |
| `tools/test_e2e_relay.py` | one-shot exec over MQTT-relay |
| `tools/test_e2e_daemon.py` | persistent daemon + `send`, both flavors |
| `tools/test_chat_friend_to_operator.py` | friend TUI chat composer + daemon-reconnect regression (drives the TUI via tmux send-keys) |
| `tools/test_repl_mode.py` | operator REPL mode |

## Tmp file lifecycle

The launcher downloads the binary to `${TMPDIR:-/tmp}/probe-$(id -u)`
and overwrites it on every invocation. No `~/.awm/bin`, no PATH
modifications. After the binary exits, the only residue is that one
file (cleared on reboot, or `rm` manually).

## MQTT-relay fallback for UDP-blocked friends

If the friend's network blocks outbound UDP (verified case: UBC's
`sockeye` HPC login node), WebRTC cannot establish a data channel and
webrtc-rs does not yet handle TURN-over-TCP. Run both sides with
`--mqtt-relay` to route Frames through the EMQX broker instead:

```sh
# friend (sockeye, etc.)
./probe --name mybox --no-consent --mqtt-relay

# operator (capella, dev box)
python3 tools/probe_op.py --mqtt-relay mybox exec 'uname -a'
```

The wire protocol (Frame JSON) is unchanged — only the carrier swaps from
the WebRTC data channel to the broker. P2P privacy is lost (the broker
sees stdout/stderr). See `PROTOCOL.md` for topic and trade-off details.
Verified end-to-end against sockeye on 2026-05-26 — transcript in
`.awm/sockeye_verification_transcript.md`.

## Phasing reminder

- **Current**: WebRTC data channel + one-shot `Exec` frames. EMQX-based
  signaling. Decoupled from awm.
- **Next**: interactive pty (`PtySpawn`/`PtyData`/`PtyResize`/`PtyExit`
  frames). Same transport.
- **Then**: awm integration — `/vagrant/*` endpoints, browser SPA
  operator, room creation, audit-mirror.

See [`/home/tony/.claude/plans/based-on-that-plan-expressive-waterfall.md`](../../../../.claude/plans/based-on-that-plan-expressive-waterfall.md)
for the full plan.

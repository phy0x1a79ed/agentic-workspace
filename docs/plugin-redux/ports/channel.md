# Port plan: channel

## Purpose & Contents

This file holds the approved port plan for channel, which absorbs relay and tether, from the comms group's plan (task CWPYAtyb), approved by Tony on 2026-11-06. It names only what the Python does not make obvious. The Python at release 49232bf9 is the reference for everything kept. The rules every comms port follows are in `comms.md`.

## Plan

Replaces `awm/plugins/{channel,relay}/` and `service_components/tether/` (3880 lines). One plugin has two services, and one binary picks its half from `AWM_SERVICE_NAME`:
- `channel` runs on member nodes.
- `relay` runs on deneb through `profiles = ["relay-host"]`, with deneb excluding `channel/channel`. The unit instance stays `awm-plugin@relay`, since its `AWM_DIR`, user, tokens and `relay.env` derive from `%i`. Only its `WorkingDirectory` moves, to `core/plugins/channel`.

`channel issue` replaces `python -m awm.tether.issuer`. tether stays the prebuilt binaries at the `RELEASE` pin, fetched, checked and supervised in their systemd units as before. The SDK's `proc` covers this.

Surfaces are unchanged. State is read in place: both `channels.db` files, transcripts and `services/tether/`. The unit names keep `sha1(STATE_DIR)[:8]`, so the running binaries are adopted.

The plugin declares no need: under v1.13 a `reflection@1` need would refuse it on deneb. It waits with `until_provided` instead.

Tests: port the channel, relay and tether suites against a real loopback `tether-relay`, with a Rust test guest that speaks SPAKE2, and keep the how-to constants test. The live check is a room with a guest, `encrypt`, a machine session and `drain`.

Rollout, said in the land brief:
- deneb's rooms end at the cutover for certain. tether-relay runs inside `awm-plugin@relay`'s cgroup, with no user-manager linger, so the unit restart stops it. Each holder gets the room-ended notice. `offered` crypto rows reset too, since Rust spake2 cannot resume them.
- If deneb later gets a user manager, `daemon.env` is written from an allowlist, so `AWM_TETHER_ISSUE_TOKEN` never reaches disk.
- Machine sessions end once per node, because the operator daemon's issuer argv names deleted Python. That needs a quiet window.
- The order is deneb first, then mizar, then the rest.

Gotchas:
- It lands after the drop-in for deneb's relay unit (X1) and the `<plugin>/<service>` exclude.
- The `daemon.env` quoting must survive systemd 249 and 255.
- The cloud inventory and `tree.rs` change in the same batch.

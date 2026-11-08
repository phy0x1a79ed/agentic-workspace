# Port plan: channel

## Purpose & Contents

This file holds the approved port plan for channel, which absorbs relay and tether, from the comms group's plan (task CWPYAtyb), approved by Tony on 2026-11-06. It names only what the Python does not make obvious. The Python at release 49232bf9 is the reference for everything kept. The rules every comms port follows are in `comms.md`.

## Plan

Replaces `awm/plugins/{channel,relay}/` and `service_components/tether/` (3880 lines). One plugin has two services, and one binary picks its half from `AWM_SERVICE_NAME`:
- `channel` runs on member nodes.
- `relay` runs on deneb through `profiles = ["relay-host"]`, with deneb excluding `channel/channel`. The unit instance stays `awm-plugin@relay`, since its `AWM_DIR`, user, tokens and `relay.env` derive from `%i`. Only its `WorkingDirectory` moves, to `core/plugins/channel`.

`channel issue` replaces `python -m awm.tether.issuer`. tether stays the prebuilt binaries at the `RELEASE` pin, now `core/plugins/channel/RELEASE`, which the binary embeds. The binary does the fetch and the check itself, so `fetch-release.sh` is gone, and supervises the binaries in their systemd units as before. `daemon.env` is written from an allowlist with no secret in it. A secret reaches the unit through `systemd-run --setenv`.

Surfaces are unchanged. State is read in place: both `channels.db` files, transcripts and `services/tether/`. The unit names keep `sha1(STATE_DIR)[:8]`, so the running binaries are adopted.

The plugin declares no need: under v1.13 a `reflection@1` need would refuse it on deneb. It waits with `until_provided` instead.

Tests: port the channel, relay and tether suites against a real loopback `tether-relay`, with a Rust test guest that speaks SPAKE2, and keep the how-to constants test. The live check is a room with a guest, `encrypt`, a machine session and `drain`.

Rollout, said in the land brief:
- deneb's rooms end at the cutover for certain. tether-relay runs inside `awm-plugin@relay`'s cgroup, with no user-manager linger, so the unit restart stops it. Each holder gets the room-ended notice. `offered` crypto rows reset too, since Rust spake2 cannot resume them.
- The running operator daemon's `TETHER_ISSUER` names the deleted Python module, so the Rust plugin counts it stale and replaces it once it is idle. No live machine session is cut. Until the swap, an invite on that node fails.
- An encryption offer open at the cutover is dropped, and its holder gets a line saying so. Keyed and sealed rooms carry over as they are.
- The order is deneb first, then mizar, then the rest.

Gotchas:
- It lands after the drop-in for deneb's relay unit (X1) and the `<plugin>/<service>` exclude.
- The `daemon.env` quoting must survive systemd 249 and 255.
- The cloud inventory and `tree.rs` change in the same batch.

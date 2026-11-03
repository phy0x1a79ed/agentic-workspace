# redux-core plan

## Context

The redux rewrites the core of awm in Rust (`install.md` R112). This lane builds the build container, then the core: one binary that holds the bootstrap, the CLI, the gateway with its plugin loader, and the MCP proxy. The gateway must run today's Python services unchanged, so each service can move to the plugin contract one at a time. The contract that other lanes build against is `docs/redux/drafts/plugin-contract.md`.

## What you said

> Build, in this order: 1. mizar's Rust build container with a cargo cache volume (R113). 2. The core as a Cargo workspace in your worktree: the bootstrap binary, the CLI, the gateway with its plugin loader, and the MCP proxy (R112). 3. Usage per plugin in the gateway (R132). 4. Tests that start the gateway in a temp dir, load a fake plugin and a real Python service, and route an MCP call and a CLI call through it.

> It must load today's Python services through that contract unchanged, so the swap can go one service at a time.

> Keep the MCP surface compatible with today's (tools with verb and args, describe, the peer key).

> Local updates only, bring the peers up to date in one sweep when we are done

## Issues

- I1. No node has a Rust toolchain, and no build container exists.
- I2. The core is Python, and nothing in Rust can load a plugin.
- I3. Nothing reads a plugin manifest, so a plugin cannot ship its tools, pages and dependencies in one folder.
- I4. The usage tally counts only MCP calls, so pages, CLI use, direct code use and background runs go uncounted.
- I5. No test starts a gateway with a plugin end to end.

## High-level goals

- G1. Rust builds run in a container on mizar and stay fast.
- G2. One Rust binary does the jobs of today's gateway, CLI and MCP proxy, for the parts other lanes need first.
- G3. Today's Python services run under the Rust gateway with no change.
- G4. The gateway shows which plugins are used, by the way each one is used.
- G5. A test proves the whole path works.

## Acceptance criteria

- `scripts/rust-build.sh` builds image `awm-rust-build` and runs cargo in it with a named volume `awm-cargo-cache`. A second build of an unchanged tree takes under 30 s.
- `core/` is a Cargo workspace with `members = ["crates/*"]`. `cargo build --release --target x86_64-unknown-linux-musl` gives one static `awm` binary.
- `awm gateway serve --state <dir>` loads plugins from its configured roots. It reads `awm-plugin.toml` and legacy `run.sh` folders. It refuses a tool-name clash, a UUID clash, a missing needed UUID, a reserved name outside a core root, and a path that leaves the plugin.
- The gateway speaks the service wire protocol of the contract: register, control WebSocket, ready, call, reply, shutdown, usage frame. `/svc/<service>/fn/<fn>` works for today's service-to-service calls.
- `GET /tools?view=domains` returns one tool per domain with input `{verb, args, peer}`. `verb = "describe"` answers verb schemas. `peer` naming a configured peer forwards the call there.
- `awm mcp` answers MCP `initialize`, `tools/list` and `tools/call` over stdio.
- `awm <tool> <verb> --key value` runs a verb and prints its result.
- Usage events for `mcp`, `cli`, `http`, `plugin`, `page`, `code` and `background` land in `<state>/usage/<date>.jsonl`. `awm usage` prints the tally per plugin and surface.
- `cargo test` in the container starts the gateway in a temp dir with a fake plugin and the real Python `journal` service, and routes an MCP call and a CLI call to each.

## Tasks

- T1. Build the Rust build container and its cache volume (G1)
- T2. Lay out the Cargo workspace and the shared protocol crate (G2)
- T3. Build the plugin loader (G2, G3)
- T4. Build the gateway: hub protocol, supervisor, catalog, invoke, pages (G2, G3)
- T5. Commit a checkpoint
- T6. Compact
- T7. Build usage per plugin (G4)
- T8. Build the MCP proxy, the CLI and the bootstrap (G2)
- T9. Write the end-to-end tests (G5)
- T10. Commit, journal and report to the coordinator
- T11. Debrief

## Approach by task

### T1. Build the Rust build container and its cache volume

Add `core/Dockerfile` from `rust:1-bookworm` with the musl target and `musl-tools`. Add `scripts/rust-build.sh`, which builds the image when absent and runs cargo with the worktree mounted at its host path, the volume `awm-cargo-cache` on the cargo registry and a `target` folder kept in the worktree. The script mounts the host's `~/miniforge3` read only, so the tests can start a real Python service. This is a local image and volume, not a host change.

Gotchas: rootless Docker maps uid 0 in the container to Tony's uid, so files in `target/` stay his. Conda Python needs glibc, which bookworm has.

### T2. Lay out the Cargo workspace and the shared protocol crate

Crates: `awm-proto` (manifest types, wire frames, usage event, `CallContext`), `awm-gateway`, `awm-mcp`, `awm-cli` and the `awm` binary crate that joins them. The trust lane adds `awm-trust` and `awm-edge` under `crates/` on its own branch.

Gotchas: keep `awm-proto` free of async deps, so other lanes can depend on it cheaply. Use rustls only, so the musl build needs no OpenSSL.

### T3. Build the plugin loader

Scan each root in order. Read `awm-plugin.toml`, or synthesise a legacy plugin from `run.sh` and `service.toml`. Apply the install config's `plugins`, `exclude` and `rename`, the reserved list, the clash rules and UUID resolution. Placement lives in the install config, not the manifest (`fleet.md` R140). Write `resolved-tools.json` per plugin and pass its path as `AWM_RESOLVED_TOOLS`. Resolve `implements` and `needs.contract` (R117 item 4, Tony chose option a).

Gotchas: legacy UUID is `uuid5(NAMESPACE_URL, "awm:tool:<name>")`, the same as the plugins lane. A page with no `dir` resolves by name in the page roots.

### T4. Build the gateway

axum on loopback. Routes: `/status`, `/tools`, `/invoke`, `/usage`, `/hub/service/register`, `/hub/service/control/{id}`, `/hub/resolve/{uuid}`, `/svc/{service}/fn/{fn}`, `/p/{plugin}/{page}/...`. The supervisor starts each enabled service with `bash run.sh`, respawns after a 10 s silence, and sends `shutdown` on stop. The catalog projects each `ready` manifest into `<tool>_<verb>` and collapsed domain tools. `PeerTransport` is a trait with an HTTP implementation.

Gotchas: a duplicate register gets 409. A call to a tool that is not up waits up to 10 s, then fails 503. The Python adapter sends `ready` before its `on_start` finishes, so calls may be slow at first. Out of scope for tonight: filehop, aliases, mode gates, the private gate, overlays and dev shadow, sessions and bridges.

### T5. Commit a checkpoint

Commit the workspace with passing unit tests.

### T6. Compact

Handoff: the contract file, this plan and `core/` are on disk. The todo list holds the task state. Re-read `build-rules.md` and `lanes.md`, then continue with T7.

### T7. Build usage per plugin

The gateway records each `/invoke` by `X-Awm-Surface` and `X-Awm-Plugin`, each HTML GET under `/p/`, each `usage` frame, and each `POST /usage`. It appends JSONL per day and keeps a 60-day tally. `GET /usage` returns the tally.

Gotchas: a plugin-to-plugin call counts for both plugins. Asset GETs under `/p/` are not visits.

### T8. Build the MCP proxy, the CLI and the bootstrap

`awm mcp` is a stdio JSON-RPC loop that reads `/tools?view=domains` and posts `/invoke` with `X-Awm-Surface: mcp`. It follows a `peer_redirect`. `awm <tool> <verb>` parses `--key value` pairs, with JSON values where they parse. `awm bootstrap` writes the state dir and a default config and prints what is still manual.

Gotchas: MCP clients send `notifications/initialized` with no id, which gets no reply.

### T9. Write the end-to-end tests

An integration test builds a temp state dir with three roots: a fake plugin written as a bash plus Python-free stub is not possible, so the fake plugin is a tiny Rust test service binary built in the workspace. The real Python service is `journal`, started through `DEV_PYTHONPATH` with the host's conda env. The test routes an MCP `tools/call` through `awm mcp` and a CLI call through `awm`. It also loads `mic` from the plugins worktree when that folder exists, to check the manifest parse.

Gotchas: the journal service writes its DB under `AWM_WORKSPACE`, which the test points at the temp dir. Skip the Python part with a clear message when conda is absent.

### T10. Commit, journal and report to the coordinator

Commit on `feat/redux-core`. Post a journal entry. Send one line to job 8162a704.

### T11. Debrief

Run the `debrief` skill, then `retire`.

## Callouts

- Tony chose option a for an interface served by several plugins (R117 item 4, feat/redux 840daeae). The contract's § Contracts holds it.
- Python discovery now scans `awm/plugins` as a built-in root, commit 38dc0f95, for the lanes that add core plugins there.
- The plugin contract is at v1.5 in `docs/redux/drafts/plugin-contract.md`. Since v1 it gained the caller trust frame, the refresh hook, `platform` and `gpu`, shared tools for legacy folders, `[[external]]` services and the `[fleet]` table.
- The Rust gateway needs `awm.gatewayclient` from this branch. Each spawned service gets a fresh token at `AWM_HUB_TOKEN_FILE` and sends it on register. An older adapter gets 403. The services themselves run unchanged.
- A service under another user's systemd unit (R99, R107) dials the gateway as today. The install config lists it in `[[external]]` with a token file, and the supervisor never spawns it.
- `feat/redux-trust` builds `EdgePeers` on this branch's `PeerTransport` and changes the `gateway serve` arm. It merges cleanly with this branch, and every test passes on the merge. The final sweep joins the two.
- `[[peer]] url` in the install config is the HTTP test transport only. Real peers come from the edges file through `EdgePeers`.

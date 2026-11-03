# plugin-core

## Purpose & Contents

This doc holds the review of the awm core and its plugin system: how they are built today, where the build and `docs/redux/install.md` R112 to R119 and R132 to R135 disagree, and each decision Tony makes on them. The settled decisions go into `install.md` as requirements.

## As built

Surveyed read only on mizar, 2026-11-03.

- **Deployed (`release`, same as `dev`).** The core is the Python gateway. It finds plugins by scanning `awm/services/` and `AWM_SERVICE_ROOTS` (`awm/gateway/awm/gateway/hub/discovery.py:123`). 43 service folders each carry a `service.toml`. No `awm-plugin.toml` exists.
- **The Rust core (`feat/redux-core`, 18 commits).** A Cargo workspace in `core/` builds one static musl binary of 8.8 MB in the `awm-rust-build` image. It serves the gateway, the MCP proxy, the CLI and `bootstrap`. No live service runs on it. No RAM measurement exists.
  - **The manifest.** `awm-plugin.toml` declares the plugin, its services, its tools by UUID and major, its pages and its `[needs]` (`docs/redux/drafts/plugin-contract.md`, v1.7). A folder with only `run.sh` loads as a legacy plugin.
  - **Loading.** The loader scans the core roots first and applies the install config. It refuses a plugin for a tool name or UUID clash, a reserved name outside a core root, a missing needed UUID or major, a path outside the plugin folder, or a wrong platform. Refusals cascade (`core/crates/awm-gateway/src/loader.rs:200,299,405`).
  - **Dependencies.** Names resolve once, at gateway start, into `resolved-tools.json` per plugin (`gateway.rs:99-118`). A rename takes a gateway restart.
  - **Processes.** Each service runs as `bash run.sh` on the host with its own token, backoff and a crash limit (`supervisor.rs:117-147`). No container path exists.
  - **Surface.** `/tools`, `/invoke`, `/mcp`, `/usage`, the `/hub/*` routes and the legacy `/svc/` routes. Pages serve at `/p/<plugin>/<page>/` on the gateway's own origin (`server.rs:32-46`). MCP keeps one tool per domain.
  - **Not built.** Install from git, commit pins, passkey approval, enforcement of `needs.secrets` and `needs.network`, the `refresh` runner, aliases, mode gates and overlays.
  - **Tests.** 21 Rust tests and an end-to-end test of the isolated door pass.
- **The plugin set (`feat/redux-plugins`, 9 commits, 110k lines removed).** It drops the R134 set. 30 service folders remain. 23 carry `awm-plugin.toml`, and 7 stay legacy. The Python gateway reads the manifest. Only a test checks the UUID and needs rules (`awm/gateway/tests/test_plugin_contract.py`). Every manifest is `trust = "own"`.
- **The harness (`feat/redux-harness`).** cch replaces cx and keep. dsh serves the DeepSeek harness. Both implement one contract, and `reflection` needs only that contract. Isolated sessions run in rootless Docker.

## Differences from the design

- **R112.** The Rust core exists only on a lane. Every node runs Python. `bootstrap` writes a state folder and a config, then prints the next steps.
- **R114.** `origin` is `altair:…/awm/.bare`, not the GitHub remote.
- **R115.** No plugin installs from git, and no plugin is a project of its own. Pages still build into `awm/pages/`. A plugin has a version string and no commit pin. Every Python service shares the `awm` env.
- **R116.** The guards are not enforced: secrets, network, commit pin, passkey, own page origin, and a contained third-party plugin. The contract adds an exception that lets legacy folders share a tool name.
- **R117.** The gateway, reporting and transcripts still import `awm.claudedaemon` (item 1). Names resolve at start into a file, not into the dependent's build (item 3).
- **R118, R119.** Nothing is built: no install test, no fake fleet, no images.
- **R134.** The auth migration did not run. The `awm_penpot` table stays.
- **R135.** reflection's own-session verbs still read the caller's pid, so the isolated door cannot use them. An OpenCode exporter remains in the gateway.
- **Contradictions in the docs.**
  - R115 makes each plugin a project of its own, and R115 item 4 keeps the core plugins in the awm repo.
  - R114 and R118 name GitHub as the canonical repo, and `routines.md` R31 names mizar.
  - `plugin-contract.md:192` cites R117 item 6 for the start order rule, which is item 7.

## Decisions

- 2026-11-03: A plugin that every node installs, or that gates trust, is core and stays in the awm repo. A plugin that only some nodes install is a project of its own (`install.md` R115 item 4, `routines.md` R33). Tony: "Yes, good choices".

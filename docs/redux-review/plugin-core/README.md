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

- 2026-11-03: The core plugins stay in the awm repo. They are the plugins every node installs, the ones that gate trust, and rlm-browser, vpn, dev, transcripts, dsh, social, tether, mic and kb. Every other plugin is a project of its own (`install.md` R115 item 4, `routines.md` R33). Tony: "Yes, good choices", "the browser VPN, dev, transcripts, DSH, and social, I'd argue, are also core plugins." and "Tether and mic should also be core."
- 2026-11-03: The standard install contract is the point. Every plugin loads only through its manifest, from a folder or a git repo. Moving the optional plugins out comes second (`install.md` R115 item 4). Tony: "It's more so that we have a standardized install contract so that we can have services be installable from a folder or Git repo."
- 2026-11-03: kb is a core plugin. It serves papers into the new search as one source and keeps `kb hop` as its one direct call. (Superseded 2026-11-04: search's `hop` argument walks kb's links, and kb publishes no tool. See `search-filters/README.md`.) Only Cognee goes (`install.md` R134, `fleet.md` R136 item 3). Tony: "I accept your recommendations, but let's keep KB as a core service."
- 2026-11-03: A plugin change runs hot, with no gateway restart (`install.md` R144). Tony: "Now, plugin changes should run hot without gateway restarts."
- 2026-11-03: A tool name is unique across the fleet and is the tool's permanent id. The plugin's manifest declares it, so a fork renames by editing its manifest. The tool UUIDs and the install config rename go. The gateway looks a name up at each call in one live table (`install.md` R116 item 5, R117 items 2 to 4). Tony: "You know, I'm thinking that names should just be unique" and "We can make the name declared in a file so that forks can rename".

## Follow-ups for the build

- Give the legacy folders a manifest: auth, board, reporting and search. cch replaces cx and keep, and the harness lane gives reflection its manifest. Then drop the legacy `run.sh` path and its shared-name exception.
- Make the Python gateway enforce the manifest rules at load, not only in `test_plugin_contract.py`, until the Rust core takes over.
- Build the install from a git repo (R115, R116 item 2).
- Replace the tool UUIDs with unique names: drop `uuid` from the manifests, `resolved-tools.json`, `AWM_RESOLVED_TOOLS` and `/hub/uuids`. Name contracts, such as `harness@1`. Drop `rename` from the install config (R117).
- Load, update and remove a plugin without a gateway restart (R144).
- Enforce the R116 guards: the manifest's secrets and network, the commit pin with passkey approval, a page origin per plugin, and a container for a third-party plugin.
- Build the install test and its images (R118, R119).
- Run the auth migration that drops `awm_penpot` (R134).
- Let reflection's own-session verbs work through the isolated door, not the caller's pid. Remove the OpenCode exporter from the gateway (R135).
- Fix `plugin-contract.md:192`, which cites R117 item 6 for the start order rule. The rule is item 7.

## Build result

Reported by the redux-plugins lane on 2026-11-03. Local commits only. `feat/redux-core` is at 21c2b302: 48de7a4a, 341dd669, 21c2b302. `feat/redux-plugins` is at 34fd7bf4 and carries all of core: 079d3f7c, 264fe6cf, 14639f7b, 3ad110fa, 3f897b14, 973da43b, 34fd7bf4.

- **Built.**
  - Unique tool names in place of UUIDs, and contracts written `name@major` (contract v1.8). One live name table, read on every call.
  - Hot reload (`awm reload`): an unchanged plugin keeps its pid, and peers hear the change.
  - A service gets only its `needs.secrets`, and none of the gateway's env. Install from a folder rolls back as one change, with pins in `installed.toml` (v1.9). The git source and the passkey approver are seams that refuse for now.
  - The install test, with images and a fake fleet. Python discovery enforces the contract at load.
  - auth and search manifests, the `awm_penpot` drop (auth schema v6), and the OpenCode exporter removed.
- **Tests pass.** Rust workspace green 15 runs in a row, `test/install/run.sh` passes under rootless docker, gateway 1315, auth 92, search 112.
- **For the trust lane.**
  - `Caller.scope` must be added to its struct literals.
  - `PeerTransport::tools()` now reads the peer's `GET /hub/table`.
  - `table_changed` must post `/hub/table/changed` over the edge, or peers keep stale tables.
- **Open for Tony.**
  - Python discovery scans `awm/plugins` as a built-in root (38dc0f95).
  - The isolated door's port 8765 and its fixed ceiling list.
  - auth adds verbs to the core `awm` tool, or moves to its own `auth` tool.
  - Which quay org the test images use. They are tagged `quay.io/hallamlab/awm-test-*:dev` and not pushed.
- **For the cleanup pass.**
  - The legacy branches that go at the cutover: `loader.rs:221,427`, `manifest.rs:166`, `discovery.py:366,632` and `test_plugin_contract.py:25`.
  - `isolated.rs:18` hard-codes `harness-cc`.
  - Cross imports: claude-science and dsh import httpsfront, reporting imports board, scopes imports dvc.
  - The `/svc` door (`server.rs:47,237`), and auth's `/svc` call to reporting (`auth/service.py:319`).
  - `scopes.py:507,522,604` still write `mcp-opencode.json`. `AGENTS.md:5` and `mcp_server_sdk.py:63` still name OpenCode.

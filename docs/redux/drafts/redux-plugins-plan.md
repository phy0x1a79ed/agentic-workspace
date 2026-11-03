# redux-plugins plan

## Context

The redux trims awm's plugin set to what Tony uses and makes every service a real plugin that carries its own manifest. This lane removes the dropped services from the tree, gives each kept service a manifest and guards in the format agreed with redux-core, and adds the plugin side of the new usage counts. Everything lands as local commits on `feat/redux-plugins`. Nothing reaches a host tonight.

## What you said

> Others can drop

> Drop cognee

> Mic is needed to get  audio into remote boxes.

> Vpn is required for sockeye ssh

> Fileviewer also required and should be core. I visit, not agents.

> Engcyc runs on showtime. Uninstall everywhere else.  Altair may keep for dev

> plugins should be installable from a git repo directly and not require any additional code in awm itself, like registering services or pages.

> Pages need a visit count instead of a call count

From the coordinator's brief: "Keep kb's papers index, vectors, hop arrays and encoder in place", "Do not uninstall anything on a host" and "Run the full Python test suite after the removals and fix what they break."

## Issues

- I1. Penpot, drawio, graphify, compute, stt, virtmic, hpcllm, notes, writing, tts, hermes and kb's Cognee parts still sit in the tree, and docs, configs and tests still name them.
- I2. No service carries a manifest that says what it serves, what it needs and which nodes run it, so none can load as a plugin from outside `awm/services`.
- I3. Nothing keeps a plugin from importing another plugin's code or from reading secrets it never declared.
- I4. Usage counts see only MCP calls, so background runs and direct use from other projects' code leave no trace.

## High-level goals

- G1. The tree holds only the plugins Tony keeps.
- G2. Each kept plugin's placement reaches the fleet's install config, so a deploy puts it on the right nodes. The manifest names no host (`fleet.md` R140).
- G3. Each kept plugin loads from any folder through its own manifest, inside the guards the plugin contract sets.
- G4. Each plugin reports how it is used in the way that fits it.

## Acceptance criteria

- `git ls-files awm/services awm/pages` lists none of the dropped services or their pages.
- `git grep -i -w` for each dropped name outside `docs/redux/` returns only intentional history, such as a migration note or a changelog line.
- `awm/services/kb` has no Cognee code path. `kb_export`, `papers.py` and the `[[archive]]` kb source are unchanged.
- Every kept service folder holds `awm-plugin.toml` in the format agreed with redux-core and no `service.toml`.
- No manifest names a host. redux-build/fleet holds the placement: rlm-factorio on mizar, engcyc on showtime with altair for development.
- The Python gateway reads `awm-plugin.toml` for tool, tier, profiles and archive, and still reads a legacy `service.toml`.
- A guard test fails on a kept plugin that imports another plugin's package, reads an undeclared secret, or does not load from a root outside `awm/services`.
- `awm.usage` sends a background-run event over the control WebSocket and a code-use event by `POST /usage`, in redux-core's v1 event format. The Python gateway writes both to the day's JSONL file.
- The full Python test suite passes, or every remaining failure is listed with its cause and shown to predate this branch.

## Tasks

- T1. Write and commit this plan (G1, G2, G3, G4)
- T2. Delete the dropped services and the pages and UI components only they use (G1)
- T3. Strip Cognee from the kb service and keep its search parts (G1)
- T4. Remove every reference to the dropped plugins from docs, configs, gateway code and tests (G1)
- T5. Run the full Python test suite, fix what the removals break, and commit (G1)
- T6. Compact
- T7. Teach the Python gateway's discovery to read `awm-plugin.toml` (G3)
- T8. Write `awm-plugin.toml` for each kept service and delete its `service.toml` (G2, G3)
- T9. Add the guard tests (G3)
- T10. Commit a checkpoint
- T11. Compact
- T12. Build the `awm.usage` library and the Python gateway's sink for it (G4)
- T13. Report background runs and code use from the kept plugins (G4)
- T14. Run the full Python test suite and commit (G1, G3, G4)
- T15. Draft the doc updates (G1, G3, G4)
- T16. Debrief

## Approach by task

### T1. Write and commit this plan

Commit this file alone on `feat/redux-plugins`.

Gotchas: none.

### T2. Delete the dropped services

Remove `awm/services/{penpot,penpot-plugins,penpot-view,drawio,graphify,compute,stt,virtmic,hpcllm,notes,writing,tts,hermes}` and `awm/pages/{drawio,hermes,notes,stt,tts}`. Remove a `ui_components` folder only when no kept page imports it, such as `stt-composer`, `stt-telemetry` and `tts-history`. Delete with `git rm`. Touch no host install.

Gotchas: a kept page may import a dropped UI component, so check every `@awm/<name>` import first. A kept service may import a dropped service's package, compute's `PROTECTED` list in particular.

### T3. Strip Cognee from kb

The kb service supervises the kb server from `projects/kb`, which is the Cognee server. The new search may keep a resident server for the encoder, so the supervisor itself is search-finish's call. I asked search-finish (job 6a450f72). Until it answers, remove only what is Cognee by name: the `graph` and `answer` recall modes, `search_type`, the OpenRouter key handed to the child, and the `import cognee` check in `install.sh`, with their docs and tests. Leave `papers.py`, `kb_export`, the `[[archive]]` table and the supervisor.

Gotchas: search-finish may edit the same files on `feat/svc-kb`. Keep the cut small so the merge stays easy. Never touch `projects/kb` or `awm/services/search`.

### T4. Remove every reference

Grep each dropped name with word boundaries across the tree, `docs/redux/` excluded. Fix `AGENTS.md`, `PROTOCOLS.md`, `ARCHITECTURE.md`, `README.md`, `FEDERATION.md`, the gateway's tool lists and seeds such as `usage_seed.txt`, `.mcp.json` templates, deploy scripts, and tests. Read each hit, because "notes", "writing", "compute" and "tts" are also plain words.

Gotchas: the workspace `AGENTS.md` that sessions load is the deployed copy. Edit the repo copy only. The global `~/.claude/CLAUDE.md` names `graphify` too, but it is host state that every live session reads, and graphify still serves until the sweep, so it waits for the sweep.

### T5. Run the full Python test suite

Run every service's and the gateway's tests in the `awm` env. Fix what the removals broke. Record failures that also fail on the base commit as pre-existing. Commit the removals.

Gotchas: some tests need a live gateway or network. Mark those pre-existing rather than chase them.

### T6. Compact

Handoff: the plan on disk, the removal commit, the todo list, and the two open replies: redux-core (job a92d02bf) on the manifest additions, and search-finish (job 6a450f72 on altair) on the kb supervisor.

### T7. Teach discovery to read the manifest

In `awm/gateway/awm/gateway/hub/discovery.py`, read `awm-plugin.toml` when a folder has one. Map its `[[service]]` and `[[tool]]` entries onto the keys `service.toml` gives today: tool, tier, profiles and archive. Keep `service.toml` as the fallback. Placement is not the manifest's job (`fleet.md` R140). It went to redux-build/fleet for the cloud project's install config.

Gotchas: a manifest that does not parse must not switch a tool off. Fall back to the defaults, as `service.toml` does today.

### T8. Write the manifests

Write `awm-plugin.toml` for every kept service from a one-off script that reads each `service.toml` and each `API_MANIFEST` tool name. Give each tool `uuid5(NAMESPACE_URL, "awm:tool:<name>")`, written once and then permanent. Declare secrets, network and the tools each plugin calls. Name no host. Declare a platform or a GPU need where a plugin has one. Delete each `service.toml`. Keep the manifests of the harness services (reflection, cx, keep, dsh) minimal and tell redux-harness they own them. Leave `awm/services/search` to search-finish.

Gotchas: a tool's MCP name can differ from its folder, as `scopes` serves `scope`. Read `tool` from `service.toml`, not the folder name.

### T9. Add the guard tests

Add one test module that walks every service folder and checks four things. The manifest parses and fills the contract. No package imports another plugin's package, shared libraries in `awm/service_components` excepted. Each secret-shaped environment name the code reads appears in `needs.secrets`. A copy of the folder under a temporary `AWM_SERVICE_ROOTS` root discovers with the same tool, tier and placement.

Gotchas: services that import `awm.claudedaemon` are R135 debt that redux-harness removes. Allow that one import by name, with the reason.

### T10. Commit a checkpoint

Commit T7 to T9 together.

### T11. Compact

Handoff: the manifest format as agreed, the converted services, the guard test's path, and what redux-core answered.

### T12. Build the usage library and sink

Add `awm/service_components/usage` with `record_background(job, ok, ms)`, which sends the control-WebSocket frame `{"kind": "usage", ...}` through `ServiceAdapter`, and `record_code(plugin, name)`, which posts the v1 event to `/usage`. Neither raises. Give the Python gateway a handler for that frame and a `POST /usage` route that append to `<AWM_DIR>/usage/<YYYY-MM-DD>.jsonl`. Leave the gateway's own recording of MCP, CLI and page calls to redux-core.

Gotchas: a usage write must never fail or slow a call. Write each event as one append under `PIPE_BUF` bytes.

### T13. Report usage from the kept plugins

Wrap the background loops of transcripts (sweep and nightly), rlm-factorio (lab runs) and dev, mic and vpn where they run loops, with `record_background`. Give fxlab-style callers one call to make, and show it in rlm-factorio's `INSTALL.md`. Do not edit game-bot.

Gotchas: a loop that ticks every few seconds floods the log. Report a run of real work, not an idle tick.

### T14. Run the full Python test suite and commit

As T5, with the new tests included.

### T15. Draft the doc updates

Update `ARCHITECTURE.md` and `PROTOCOLS.md` where the manifest replaces `service.toml` and where usage events land. Put the doc changes for `docs/redux/` and the global `CLAUDE.md` in *Callouts* for the interview session.

### T16. Debrief

Run the `debrief` skill. Message job 8162a704 on mizar with one line.

## Callouts

- virtmic folds into mic (Tony, 2026-11-03), because mic's bridge streams into virtmic's PulseAudio sink. The fold is its own commit, so it reverts alone.
- The compute Claude Code hook stays installed on mizar after the sweep. It fails open and does no network call. redux-build/fleet has the cleanup.
- `awm/package.json` still lists the editor dependencies only the notes page used. The sweep prunes them with npm, which this host lacks.
- The global `~/.claude/CLAUDE.md` and the deployed workspace `AGENTS.md` still send agents to `graphify`. Edit them in the sweep that deploys this branch, not before.
- `install.md` § Gap from today, "R134 broken", becomes true once this branch lands. The interview session owns that doc.

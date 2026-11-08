# Trilium design — build plan

## Purpose & Contents

This file is the build plan for the decisions in `DECISIONS.md`, for Tony's approval before any build starts. It names what this scope builds, what it hands to plugin-redux for its Rust ports, and how the work reaches plugin-redux/interview. The spec items in `docs/redux/` stay the source of truth for each feature.

## Context

The trilium-design discussion settled a new board, a swarm status page, new agent buttons, seat switches, one sign-in and per-task approvals. Most of the code behind these sits in plugins that plugin-redux is porting to Rust right now: operations and auth (port-ops), social (port-comms) and reflection (port-harness). This plan builds only what no port touches, in this scope, and gives the rest to plugin-redux/interview as a written contract, so nothing collides.

## What you said

> please submit formal plan before starting and work in your own scope so you literally can not collide. the last step would be to just submit work to plugin-redux/interview agent, who will merge along with their work

> plan once whole stack is done

> maybe we need a telemetry core plugin that gathers data and operates the board? can we build this in rust?

> I want ops and the control panel to be pages in trillium, essentially notes

## Issues

- I1. No part of awm gathers a roster of nodes, sessions, seats and services, so the board goes stale when sessions die unreported.
- I2. The board shows Proposed, Done, a null body, a repeated title and no claude.ai links, and it lacks Stale, History, New agent, a resizable divider, click-to-copy ids and per-task approvals.
- I3. Ops holds only notices and portal links, with no node cards, seats section or services section.
- I4. No note can start a task from itself.
- I5. The board's buttons are frames from alcor with a hash chain, and the sign-ins differ per surface.
- I6. The Rust ports plan none of these features, and the operations port plans to move the board writer into trilium, which is the same code this work rewrites.

## High-level goals

- G1. The swarm keeps a live, event-driven picture of its nodes, sessions, seats and services.
- G2. Tony runs the swarm from Trilium notes: the board, Ops, and a bot button on any note.
- G3. One sign-in covers every surface, and only approvals need the passkey.
- G4. The work lands through plugin-redux with no collision with its ports.

## Acceptance criteria

- `core/plugins/telemetry/` builds in mizar's container on the SDK in `core/crates/awm-plugin-sdk`, and its tests pass. On a session start or stop event its roster changes within seconds, with no timer in its code.
- `telemetry nodes` returns each node with role, last seen, sessions with claude.ai links, 7-day session count, and the services it can use. A services view lists each singleton with host, health, last response and last use.
- The board writer and board page live in `awm/services/trilium/`, and its tests pass. On the fork's dev server on mizar, against a vault snapshot, the board shows Needs you, Working, Stale and History, with no Proposed section, no Swarm triage folder, no null body and no repeated title. The board has a working divider, click-to-copy ids, claude.ai links and an approvals list per task.
- The Ops note renders node cards, a seats section with switches, a services section and a fuzzy search, from telemetry data.
- Every ordinary note has a bot button beside Trilium's create-child button. A card's note has none. Each press calls the alcor API in the contract. The marker note and the clone appear as `queue.md` R63 item 3 says.
- No vault page embeds a frame from alcor, except the sign-in and approval modal.
- `docs/trilium-design/CONTRACT.md` names every endpoint, verb, event topic and spec item that each port must add, one item per port, and plugin-redux/interview has it.
- Each nested scope's work reaches the land queue through `scripts/land.sh submit`.

## Tasks

- T1. Write the contract for the ports (G3, G4)
- T2. Start the two build workers in nested scopes (G1, G2)
- T3. Build the telemetry plugin in Rust (G1)
- T4. Build the vault side in the trilium plugin (G2)
- T5. Compact
- T6. Verify both workers' submissions (G1, G2)
- T7. Send CONTRACT.md to plugin-redux/interview (G4)
- T8. Post the discussion's decision journal (G4)
- T9. Report to redux-endstate/review
- T10. Debrief

## Approach by task

### T1. Write the contract for the ports

This session writes `docs/trilium-design/CONTRACT.md`. It lists, per port, what the trilium side and telemetry need from it, each tied to its spec item:
- **port-ops, operations:**
  - the HTTP API on alcor's address for vault pages, which takes the device credential and checks the origin
  - the everyday verbs: New agent, a task from a note, a seat switch, close and reject
  - the per-task approvals list
  - the approval modal
  - the Stale state and the rename of Done to History
  - the removal of note_new, spawn, new_from, the panel frames and the hash chain
- **port-ops, auth:** the 12-hour sign-in page on alcor's public address, the signed links, the redirect and return, the vault's sign-in frame, and the device-credential cookie.
- **port-comms, social:** the seat's Remote Control switch, held with the seat state and applied when the seat's session starts.
- **port-harness, reflection:** session start, stop and Remote Control change as gateway emit topics. Also a no-brief start in `awm/lobby`.
The contract holds one item per port, so each port lands its item on its own. The contract's items never go in this branch: plugin-redux/interview hands each to its port owner. `awm/plugins` covers only a plugin's install changes, so a fifth item asks the gateway, which plugin-redux/interview owns, for a service's up and down and for usage events. plugin-redux/interview agreed on 2026-11-07 that T4 takes item C1 from the operations port. It tells port-ops itself, and this session never messages port-ops.

Gotchas: the contract restates no spec text. It points at the R-items and adds only names and shapes. A shape that a port plan already fixes, such as `board_changes`, is reused, not renamed.

### T2. Start the two build workers in nested scopes

Two top-level sessions start with `reflection(verb="start_other")`, joined to task CWPYAtyb, each in a nested scope of this one:
- `awm/trilium-design/telemetry` on Opus at high effort, because it is a new Rust plugin with cross-node events.
- `awm/trilium-design/vault` on Sonnet at high effort, because it is Python and note JavaScript that follow written specs.

Each brief carries the decisions, the spec items, the contract and the rules:
- build only in its own scope
- do not merge or deploy
- report to this session

Gotchas: the two scopes share no files. telemetry owns `core/plugins/telemetry/`, and vault owns `awm/services/trilium/` and the operations board files it moves out. Each worker starts from this branch, which is based on release, so both have the SDK and the spec edits. Each worker pushes to the `github` remote, because `origin` in these worktrees is altair's stale bare.

### T3. Build the telemetry plugin in Rust

The worker builds `core/plugins/telemetry/` in the port layout: `awm-plugin.toml`, `state.toml`, `run.sh` and the crate `awm-plugin-telemetry`, with a pin in `deploy.toml`.
- The node part subscribes to the emit topics in the contract with the SDK's supervised subscriber. It reads the starting state once at start.
- The alcor part keeps the roster, the 7-day session counts and the service health. It serves `telemetry nodes` and `telemetry services`. It publishes a roster-changed topic.
- Install opt-in is a config value that names the swarm.
- It reads `GET /hub/plugins` at connect and follows `awm/plugins` for service up and down.
- It builds only through `scripts/rust-build.sh`, with dependency features set only in the workspace `core/Cargo.toml`. It runs `rust-build.sh --gc --yes` at the end of each phase, because mizar's disk filled twice this week.
- The commit that adds the plugin adds its `deploy.toml` pin line and a describe baseline under `docs/plugin-redux/ports/baseline/`.
- It lands through `scripts/land.sh submit --brief <file> --restart telemetry` from its scope. plugin-redux/interview merges as merger.

Gotchas: a store or an index engine needs plugin-redux/interview's yes first, because each heavy engine lives in one plugin (decision c2419412). The SDK's `telemetry` module is only the tracing subscriber, so the plugin's tool name must not clash with a module name in a way the gateway's name table refuses. Until reflection and the gateway emit the contract's topics, test with a fake emitter. No timer and no poll loop may stand in for the missing topics.

### T4. Build the vault side in the trilium plugin

The worker moves the writer, `board.py`, `board/` and `board-install` from operations into `awm/services/trilium/`, as the operations port's item C1 describes, and rewrites them to the decisions:
- the four sections, Stale and History, and no Proposed or Swarm triage
- no null body, no repeated title, the divider, click-to-copy ids, claude.ai links and the per-task approvals list
- the New agent button and its node dropdown
- the Ops note: node cards, seats with switches, services, and the fuzzy search, rendered from telemetry
- the bot button beside Trilium's create-child button on ordinary notes, with the marker note and the clone
- the writer roots: `tasks`, `Ops` and the board page, plus the two acts of `fleet.md` R106 item 2

Every press calls the contract's alcor API. The worker tests against a stub of that API and on the fork's dev server on mizar, against a vault snapshot, never the live vault. trilium stays Python with its own install hook and env. The worker lands through `scripts/land.sh submit --brief <file> --restart trilium` from its scope. If the change rewrites existing state, it lands only after ops' `awm-statecheck`.

Gotchas:
- The writer still runs today in the Python operations on alcor. The move must not leave two writers live at once, so the old one goes in the same change.
- Removing a clone must remove only the branch, never the note.
- Never print password note bodies.

### T5. Compact

Handoff for the resumed session:
- the two worker job ids and their scopes
- the paths of `PLAN.md`, `CONTRACT.md`, `DECISIONS.md` and `STACK.md`
- plugin-redux/interview's conventions, which this plan's T2 to T7 carry
- the todo list, which marks T1 to T4
- facts a re-read does not give: what each worker last reported, and any contract change a worker asked for

Run `reflection(verb="compact", args={followup:"continue with T6"})`.

The compact right after approval runs with a followup that pauses for Tony, at Tony's request.

### T6. Verify both workers' submissions

This session checks each worker's report against the acceptance criteria before the worker submits. It runs the tests of each plugin and the dev-server check itself. It then retires each worker once plugin-redux/interview takes its submission.

Gotchas: telemetry submits first, because the vault side reads its verbs.

### T7. Send CONTRACT.md to plugin-redux/interview

This session runs as plugin-redux/port-trilium, one of plugin-redux/interview's workers, at Tony's request. It sends `CONTRACT.md` by `SendMessage`, with the commit that holds it. plugin-redux/interview hands each item to its port owner. This scope never lands or deploys outside `land.sh submit`.

Gotchas: the branch carries spec edits across `queue.md`, `fleet.md`, `install.md` and `sessions.md`. The message names them, so the merger expects the overlap with port-ops' own spec edits.

### T8. Post the discussion's decision journal

One journal in awm/trilium-design, tagged `decision` and `closed`, with one line per decision, the path of `DECISIONS.md` and the commit, as the reworked dfs-interview skill says.

### T9. Report to redux-endstate/review

One `SendMessage` with the decision journal ids, the workers started, and the branch handed to plugin-redux/interview.

### T10. Debrief

Run the `retire` skill.

## Callouts

- The vault UI works end to end only after port-ops lands the operations API and the auth sign-in in the contract. Until then, T4 is verified against a stub and on the dev server.
- The board on the live vault keeps today's layout until plugin-redux lands this branch.

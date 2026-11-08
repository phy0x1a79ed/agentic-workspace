# Trilium design — handoff to the merge session

## Purpose & Contents

This file hands the trilium-design build to the session that merges and lands it. It lists each branch head and its state, the decision Tony must make before vault can work, vault's land order, the state of telemetry's cloud batch, and the open items of `CONTRACT.md` per port. The decisions are in `DECISIONS.md` and the plan in `PLAN.md`, beside this file.

## Branch heads

| Branch | Head | State |
| --- | --- | --- |
| awm `feat/trilium-design` | this commit | Spec edits, `CONTRACT.md`, this file. Not landed. |
| awm `feat/trilium-telemetry` | 82072cf6 | Landed in release acadb279. Live and idle on every node. |
| awm `feat/trilium-vault` | 92a7d431 | Built and tested. Not submitted. |
| cloud `feat/telemetry-optin` | 610e182 | Written and tested (85 passed). Not merged, and no play run. |

Every awm branch is on the `github` remote. The cloud branch is on `origin`, altair's cloud bare.

## Decision for Tony: deneb's outbound calls

`fleet.md` R79 says deneb opens no outbound ssh, and cloud's `test_service_keys_come_from_one_place` enforces it: deneb calls no door. The vault writer on `feat/trilium-vault` runs in trilium on deneb. It calls `operations board_changes` and `board_import`, and `telemetry nodes|services|seats`, with `peer=alcor`, and it subscribes to `operations/board` and `telemetry/roster` on alcor. Each of those is an outbound call through alcor's door, which R79 forbids. So `feat/telemetry-optin` leaves deneb out of telemetry's callers, and the vault writer can reach neither service.

Choose one before vault lands:
1. **Push from alcor (recommended).** operations and telemetry on alcor call hidden trilium verbs on deneb, such as `board_apply {cards, mark}` and `ops_render {nodes, services, seats}`, on each of their events. alcor already calls trilium through deneb's door. R79 and R147 item 3 ("renders the roster into it one way") both hold. The writer's note code on `feat/trilium-vault` stays. Only its input changes from a pull to a push, and the contract gains the two verbs.
2. **Amend R79** to let deneb call the read verbs of operations and telemetry. deneb then needs a service key: a `service_key` run on deneb, the key recorded in `service_keys.yml`, then `singleton_door` on alcor.

## Vault land order

1. port-ops' Rust operations lands first. It carries the hidden verbs the writer reads, `ops_notes`, `ops_list` and the dropped trilium need (plugin-redux/interview passed these to port-ops).
2. Apply Tony's decision above to `feat/trilium-vault`.
3. Check release for the Python board writer (`awm/plugins/operations/awm/operations/writer.py`). If it is still there, delete it, with `board.py`, `board/`, `trilium.py` and the `board-install` command, in one commit on `feat/trilium-vault`, so two writers never run.
4. Run ops' `awm-statecheck`, because the first start migrates the vault: one `board_import`, then Proposed, Done and Swarm triage move to History.
5. Submit with `scripts/land.sh submit --brief <file> --restart trilium`.

**CAUTION** trilium 0.2.0 needs `operations` major 2 and `telemetry` major 1. If trilium does not load after the deploy, check those two needs first.

Not verified on vault: the live gateway path, real alcor responses, and the clone and marker on the dev server (unit tests only).

## Telemetry's cloud batch

`feat/telemetry-optin` (610e182) does the following:
- adds `telemetry` to `edges.yml` singletons on alcor, with callers lyra, capella, mizar, altair and shogun;
- gives `awm_config` an `awm_plugin_config` that renders `[telemetry] swarm = "tony"` on each reporting node;
- adds a sandbox drop-in for `awm-plugin@telemetry` on alcor, which binds the binary, `config.toml` and `edges.yml`, and sets `AWM_INSTALL_CONFIG` and `AWM_EDGES_FILE`.

Steps not taken, each a rollout:
1. Merge the latest cloud `dev` into the branch, rerun the cloud tests, and send plugin-redux/interview the `--check --diff` for the run slot.
2. Run `site.yml --tags awm_config` on `swarm:stations:admin`.
3. Run `--tags singleton_door` and `--tags singletons` on alcor.
4. Run `arbutus.yml --tags altair` from shogun.
5. Check that `awm-plugin@telemetry` starts on alcor and that systemd creates the bind-mount targets under the `/home` tmpfs.
6. Merge into cloud `dev` and push before the slot passes on.

Open on that batch:
- The 64 MB memory cap is a guess. Measure the binary's RSS on alcor after the first start.
- The gateway's config parser is assumed to ignore an unknown `[telemetry]` table.
- deneb's caller entry waits on the decision above.

## Open contract items per port

`CONTRACT.md` at d5c3a3f2 holds each item.

- **port-ops:**
  - items 1 to 6: `card_body_set` goes, `board_import`, the sections, `board_changes`, `ops_list` and `operations/board`;
  - Stale from `telemetry/sessions`, reconciled on reconnect from `telemetry nodes`;
  - the vault API, including `POST /api/seat/remote_control {seat, on}`;
  - the `agent` order, the approval modal, the sign-in, and the removals.
  - Option 1 of the decision above adds `board_apply` calls from operations.
- **port-comms:** built at `feat/port-comms` 460f22b3 and not landed. `label` is in the `social/seats` event.
- **port-harness:** `reflection/sessions`, and `rc_url`, `task_id` and `session_id` on `list_other` (0e66664e), plus `remote_control` waiting for the end of a turn.
- **gateway** (plugin-redux/interview): `awm/services` and `awm/usage` landed in release acadb279 (ticket aa027885afb8). Check whether, on a singleton's host, `awm/usage` records the `tool` of each call through the door. telemetry's `last_response` needs it.

## Spec overlap

`feat/trilium-design` edits `docs/redux/queue.md` (R61-R66, R137), `fleet.md` (R84, R97, R106, R147), `sessions.md` (R146) and `install.md` (R115), and the dfs-interview skill under `docs/redux/drafts/claude-home/`. Expect conflicts with port-ops' edits to `queue.md` and `fleet.md`.

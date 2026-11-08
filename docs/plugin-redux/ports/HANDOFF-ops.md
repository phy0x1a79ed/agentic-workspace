# Ops group handoff to the merge session

## Purpose & Contents

This file hands the ops group's unlanded work to the session that merges it. It names the branches, the queued ticket, the land order, the cloud changes, the answered questions and the steps nobody ran. Each plugin's design lives in its own file in this folder. This file holds only what the merger needs to act. Delete it when every item below has landed.

## Branches

| Branch | Head | Holds | State |
|---|---|---|---|
| `feat/port-ops` | the commit that adds this file, on dfb00c33 | the operations port (e0e1a238) and the gateway's `[[service.front]]` pass-through (dfb00c33) | builds, 212 operations tests pass, not submitted |
| `feat/port-ops-t7` | 34521995 | the dvc port (3f3667fd) and the run-tests ORDER fix | queued as ticket `68647284ec63`, `--restart dvc,routine` |
| `feat/port-ops-t9` | a7009765 | wave 1 of the auth port, unfinished | does not build, see § T9 |
| cloud `feat/ops-units` | 7a8a009, on b81bc42 | the operations unit and caller changes | pushed to origin and github, cloud pytest 84 pass, not applied |

## Land order

1. Ticket `68647284ec63` (dvc). After it lands, run the T7 live checks in `dvc.md`.
2. Merge `feat/port-ops-t7` into `feat/port-ops`, then submit `feat/port-ops` with `--restart operations,httpsfront`. Its brief is § T11 brief.
3. Apply cloud `feat/ops-units` in the same window as the alcor rollout of step 2. Until it applies, alcor's unit runs Python from a folder that step 2 deletes.
4. Finish T9 on `feat/port-ops-t9`, then land it per `auth.md`.

## T11 brief (operations)

- Behaviour: operations runs as a Rust binary from `core/plugins/operations`, on alcor only. It keeps the operations@2 verbs. It adds the hidden verbs `board_changes`, `board_import` and `ops_list` and the emitter `operations/board` of CONTRACT.md b11adebc. The vault API on auth.tony-xy-liu.com replaces the panel. Node edges redirect to `/signin` in place of `/portal_login`. The Claude Code hook is a shell shim that runs `operations hook`. trilium owns the board writer.
- Proof: 212 crate tests. `scripts/run-tests.sh tlsfront httpsfront` passes 560 with the `/signin` redirect. On alcor, as `awm-operations`, on a `VACUUM INTO` copy: `state snapshot`, `check-state --migrate`, then `state check` reported nothing lost. `check-state` parsed all 3 passkeys and 4 key files.
- Live check: on alcor, `awm services` shows operations from `<AWM_DIR>/bin/operations`, and `check-state` and `state check` exit 0. From mizar, `awm operations task_list` answers the four sections. `/signin` serves the page. With Tony, run one passkey sign-in, one card close and one 2fa burst. `deploy-hooks.sh --check` shows `awm-operations-hook.sh` and no `.py`.
- Restart: `operations,httpsfront`. A stale edge sends a browser to `/portal_login`, which now answers 404.
- **WARNING** The first start copies the state to `.pre-rust/` and refuses to start on a failed copy. Do not delete `.pre-rust/`.
- The board stays as it is until trilium's writer runs `board_import`.
- Rollback: stop the unit, run `operations downgrade-sections`, revert and deploy. Keep the live `queue.db`, `approver.db` and `relay.db`.
- Cutover steps on alcor: `operations.md` § Cutover.

## Cloud changes

- Operations (cloud `feat/ops-units`, X1 reused):
  - `singleton_rust` gains `operations: operations`.
  - The door key's forced command runs `{{ awm_dir }}/bin/operations door --node`.
  - `edges.yml` adds deneb to the operations callers, for trilium's board writer.
- **CAUTION** deneb's service key must be in `service_keys.yml` before its calls to operations pass.
- `run-containers.sh` does not run on mizar: the `community.docker.docker` connection plugin is missing. The guard refused safely. Install the collection before relying on the container test.
- Telemetry (CONTRACT.md e62fcb43): telemetry is an `edges.yml` singleton on alcor. Every opted-in node calls it, deneb included. Every node but alcor adds telemetry to its install exclude. Each reporting node sets `[telemetry] swarm`. trilium rides in the same batch. This batch is not on `feat/ops-units`.
- T9 rollout: deneb's `awm_plugins` drops httpsfront (`inventory/host_vars/deneb.yml`). deneb's `AWM_EDGE_CERT_DIR` stays authoritative.

## Agent orders (Q-a, Q2, Q3)

- Now (c): operations keeps its own order loop, with `allowed_orders.agent = [alcor]`.
- End state (b): reflection pulls its own orders. The NO_PLUGIN gate on `order_pull` and `order_done` must admit reflection first.
- The verbs: `order_pull {node, kinds?, wait_s ≤ 55}` returns signed envelopes `{order: {order_id, kind, node, payload, not_after}, sig}`. Verify each with `awm_trust::grant::verify_order`. `order_done {order_id, ok, note}` answers `{order_id, recorded}`.
- Q2: on `issue_order`, emit the topic `operations/orders {node, order_id, kind}`. reflection pulls with `wait_s` 0 on connect, on reconnect and on each event.
- Q3: the grant key comes from `AWM_APPROVER_GRANT_PUBKEY` only.
- Handover: make `order_pull` claim orders with a `pulled_at` lease, so two pullers never both start one. Then drop operations' loop.

## T9 (auth), unfinished

`feat/port-ops-t9` holds the crate `core/plugins/auth` with vectors made from the Python.

- Done: `certs` and `tls`, 53 tests. Two changes from Python: the listener advertises ALPN `http/1.1`, so an h2-only client fails the handshake. With `AWM_EDGE_CERT_DIR` set, `.sans` is still copied once to `<state>/sans`, so deneb keeps its declared SANs after the Python folder goes.
- Partial: the token store, the session gate and the verbs, then policy, pages, landing and portal. These were stopped mid-port. Their tests and vectors are written. Several modules do not compile yet.
- Not started: the proxy core (raw path, header rules, WebSocket bridge, portal callback, logout, whoami, `ca.crt`), the httpsfront app (landing, vault, slices, tether, board with its semaphore and `MAX_FILE_BODY`, public invoke, status), the wiring (one service for both tools, front-declaration listeners, manifest, `state.toml`, CLI, cert and landing copies, the Python delete, run-tests rows, the `deploy.toml` pin) and the docs (`[[service.front]]` in `docs/redux/drafts/plugin-contract.md` and the README row).
- **CAUTION** The workspace globs `core/plugins/*` as members. Do not merge `feat/port-ops-t9` into a landing branch until the crate builds.
- Skipped as rollout steps: live parity on the mizar sandbox (port 7821), the per-node land, claude-science C2b's land, and the root-owned state inventory on alcor and deneb.

## Open questions

- `routine state check` and `routine land`, run from a scope worktree with `AWM_DIR` unset, take the scope as the workspace. `land.sh` guards against it. The bare binary does not. The SDK owns the fix.
- The SDK fails clippy's `result_large_err`. Crates lint with `-A clippy::result_large_err` until the SDK fixes it.
- `.pre-rust/` deletion waits for Tony's dated OK, 14 days after the operations land.

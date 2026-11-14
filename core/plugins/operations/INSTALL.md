# The `operations` plugin

## Purpose & Contents

This file covers what the `operations` binary runs, the state it keeps, how alcor's approver is set up, and how to check it and roll it back. The deploy places the static binary at `<AWM_DIR>/bin/operations`. The binary serves the `operations` service, the singleton door's locked command (`operations door --node <n>`), the operator's commands (`enrol-link`, `devices`), the Claude Code hook (`operations hook`), the credential check (`check-state`) and the pre-redux state check (`state`).

operations runs on alcor only, as the `operations` singleton of `edges.yml`. Every other node excludes it, and its sessions reach it through alcor's door.

## State

| Path | Holds |
|---|---|
| `$HOME/.awm/operations/queue.db` (`AWM_QUEUE_DB`) | every card, its sessions, messages and events, the Ops notices |
| `$HOME/.awm/operations/relay.db` (`AWM_OPERATIONS_RELAY_DB`) | the outbox, the read cache, the seen orders and the hook marks |
| `$AWM_APPROVER_DIR/approver.db` | devices, passkeys, requests, orders and the 12-hour sign-ins |
| `$AWM_APPROVER_DIR/keys/` | the Ed25519 grant key, the P-256 ack key and the ssh user CA |
| `$AWM_APPROVER_DIR/config.json` | the approver's settings, below |

On alcor `HOME` is `/var/lib/awm/operations` and `AWM_APPROVER_DIR` is `/var/lib/awm/operations/approver`. `state.toml` declares this state for `operations state`.

**CAUTION** The service fails closed. A missing `queue.db`, `approver.db` or key refuses the start, because a fresh one would hide every card or lock Tony out of approvals. Set `AWM_OPERATIONS_INIT=1` only for a fresh install.

## The approver's settings

`config.json` keeps the keys of the Python approver. These matter for the vault:

- `board_origins`: the vault's origins. Only these and the approver's own origin may call `/api/*`, and only these may frame the modal and the sign-in.
- `portals`: `{host: node}` for every surface the sign-in lists and grants into.
- `allowed_orders.agent`: the nodes an `agent` order may go to. The default is alcor alone, because only a node that runs the relay pulls its orders.
- `allowed_orders.retire` and `allowed_orders.service`: the nodes the Control panel may retire a session on or switch a service on. Each defaults to every node.
- `card_url`: the board's link to one card, with `{task_id}` in it. Without it, `card_url` answers null.
- `rate_max`: the everyday actions one device may take in a minute.

## Verify

1. Run `operations check-state`. It parses every key and active passkey and opens each store read-only. It exits 0 when nothing is faulty.
2. Run `AWM_DIR=<AWM_DIR> <AWM_DIR>/bin/operations state check`. It exits 0 when no pre-redux state was lost.
3. Open `https://auth.tony-xy-liu.com/signin`. One passkey tap lists every surface.

## Roll back

1. Stop the unit.
2. Run `operations downgrade-sections`. It renames `history` back to `done` and `stale` to `working`, which Python reads.
3. Revert the port and deploy.

**WARNING** Keep the live `queue.db`, `approver.db` and `relay.db`. Restore one from `.pre-rust/` only when `state check` proves damage. A restored `relay.db` replays orders, and for `approver.db` re-enrol through `enrol-link` instead.

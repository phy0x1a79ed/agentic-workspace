# Deploy routine rework

Goal: awm's deploy routine refreshes every node by a script, starts an agent only when a step fails, and cannot start a cascade of fixers. Tony drives the design over Remote Control.

Decisions: `search` for "deploy-routine decision", or journal posts tagged `decision` in awm/deterministic-refresh. The starting decision is journal 5662544f (overrides routines.md R32).

## Current

refresh — the deterministic per-node refresh.

## Waiting on the user

- refresh: confirm the design summary sent on the board (transport, fixer placement, two-deploy rollout).

## Preferences

## Stack

- refresh — "deploy protocol to be deterministic with only agentic error handling"
  - State: built in WIP commit bbdab778 (scripts/node-refresh.sh, kind `detached`, resumable `refreshing` batches, retry until a node answers, one fixer on the failed node). Not yet tested or landed.
- cascade-guard — "deploys are failing and starting cascade of agents": at most one live fixer per project; a failing deploy submitted by a fixer goes to Tony with request_human.
- project-refresh — per-project refresh config, such as stock's.

## Workers

## Closed

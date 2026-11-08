# The `telemetry` and `telemetry-node` plugins

## Purpose & Contents

This file covers how to place and enable the two telemetry plugins (`fleet.md` R147 item 1). `telemetry` keeps the swarm's roster on alcor and serves it. `telemetry-node` reports each node's sessions, services and usage to that roster. The verbs and topics are in the tool's describe and in `docs/trilium-design/CONTRACT.md`.

## Why two plugins

`telemetry` is a singleton on alcor. The loader refuses any plugin on another node that declares the `telemetry` tool, so the part on each node is a plugin of its own with no tool. One library builds both binaries, so a change to it rebuilds and restarts both.

## Placement

1. On alcor, load both plugins.
2. On every other node, add `telemetry` to the install config's `exclude`, beside the other alcor singletons.
3. List `telemetry` in `edges.yml` `singletons` with host alcor. Its `callers` are every node that reports, stations included, and deneb, which reads the roster.

**CAUTION** A node missing from the `callers` list cannot report. Its reports fail with `telemetry not installed`, and the node part logs a warning per failed report.

## Opt-in

Name the swarm in each node's install config:

> ```
> [telemetry]
> swarm = "tony"
> ```

A node part with no swarm reports nothing. A roster with no swarm refuses every report. A report whose swarm differs from the roster's is refused.

## Events, not polling

The node part reads the node's whole state only at connect: reflection `list_other`, `GET /hub/table` and `GET /hub/plugins`. It reads it again when the roster answers `resync`, when a report fails, or when a stream drops. A failed report waits for the node's next event, so a node that stays quiet stays stale on the roster until it reports again.

The roster keeps its state in `<AWM_DIR>/services/telemetry/roster.json`, rewritten whole on each change.

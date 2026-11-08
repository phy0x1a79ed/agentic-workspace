# Port plan: dsh

## Purpose & Contents

This file holds the approved port plan for dsh, from the harness group's plan (task CWPYAtyb). Tony approved it on 2026-11-06. It is the plan as approved: later changes go in the port commits and the journal. The rules every harness port follows, and the state each one carries over, are in `harness-state.md`. The describe each port is diffed against is in `baseline/`.

## Plan

**Surface.** It keeps tools `dsh` (7 verbs) and `harness-ds` (12 harness@1 verbs), and page `dsh`. Listed changes:
- harness-ds's target params gain descriptions;
- `list` answers an empty list when the fork is down. Its rows set `job` to the fork id, `alive` to `live`, `kind` to `web`, and `parent` and `transcript` to null.

A new mount at `/s/dsh/` proxies to the fork, rewrites `Origin`, streams bodies and bridges WebSockets.

**State.** `.awm/services/dsh/`, in place, plus the new spool `unsent.jsonl`. On start, dsh retargets a `$DSH_HOME` symlink only when its resolved target lies in the old `awm/services/dsh/config/`. capella's `.agent-presets` is relative, so resolve it first.

**Needs and calls.** None. reflection and `auth` are plain calls: `harness_event` (spooled when it fails) and `auth front {service}` for the `front` block of `status`.

**What it needs from auth.** A TLS origin front on port 12130, proxying to `/s/dsh/`, behind the same login. It is declared in dsh's manifest (§ Callouts). The front leaves dsh, and so do the `AWM_APPROVER_GRANT_PUBKEY` secret and `edge_material`.

**Fixes.** `stop` sticks, `send` with no text answers `ok: false`, and events get spooled.

**Session events** (trilium-design `CONTRACT.md` § port-harness, item 1). This is an addition to the ported dsh, made before its land. dsh sends `started`, `stopped` and `renamed` to reflection `harness_event` when its `events.mux` stream shows a session change. It diffs `session.list` against what it last saw. It never sends `remote_control`, and its rows carry no `rc_url`. When the fork goes down, every live session gets one `stopped`. At boot dsh sends nothing. These events go to reflection once and never into the `unsent.jsonl` spool.

**Tests.** Port the Python tests against a fork stub. For parity in the sandbox, use a copied state dir and the stub.

**Landing.** After reflection, and after interview's pages-build change, because the page moves to `core/plugins/dsh/pages/`. It does not wait for the `auth` front or the fork build: until then, `status` reports the front as not serving.

Gotchas: no node builds the fork today, so fork-RPC parity rests on the stub. The fork's stream may carry no session-created event. Then a diff on each stream reconnect and on each `session.list` that dsh already makes is the only source, so a session started and stopped between two reads goes unreported.

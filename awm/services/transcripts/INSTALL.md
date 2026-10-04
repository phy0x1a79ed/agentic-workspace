# awm-transcripts — install

## Purpose & Contents

Install and operate the transcripts service: what it needs, how to install it,
how the timer drives it, and what to do when a sweep reports failures. The
service's verbs and their parameters come from `awm transcripts --help` and
`transcripts(verb="describe")`, not from this file.

## What it does

It sweeps Claude Code session logs out of `~/.claude/projects` into a gzipped
mirror at `~/.claude/projects-archive`, and prunes that archive.

The unit is the session: `<project>/<sessionId>.jsonl` and
`<project>/<sessionId>/` move together. **CAUTION** — a project directory also
holds a `memory/` folder at the same depth a session sidecar sits at. It holds
the auto-memory. `awm.transcripts.layout.RESERVED` excludes it, and any new code
that walks the tree must go through `layout.sessions`.

## Requirements

- The `awm` env, with `awm-config`, `awm-persistence` and `awm-gatewayclient`.
- Read and write access to `~/.claude/projects` and `~/.claude/projects-archive`.

Nothing else. No network, no external binary.

## Install

```bash
bash awm/services/transcripts/install.sh
awm services restart transcripts
```

`install.sh` writes the gitignored `.runtime-env` sidecar that bakes the env's
absolute interpreter, which is what lets the gateway respawn the service under
systemd's minimal PATH.

## The daily sweep

The user timer `claude-archive-sessions.timer` runs the sweep at 03:30 local.
Its service unit calls the CLI verb. The unit and timer are tracked beside the
service in `systemd/`.

```bash
systemctl --user status claude-archive-sessions.timer
awm transcripts runs                 # what the last sweeps actually moved
```

**CAUTION** — the timer is `Persistent=true`, so a machine that was off catches
up on the next boot. A first run after a long gap sweeps a large backlog. Run
`awm transcripts sweep --dry-run` first if that matters.

## Pruning

`prune` is the only operation here that destroys data, so it is CLI and HTTP
only, never on the MCP surface, and it dry-runs unless told otherwise.

```bash
awm transcripts prune --days 180                 # reports, deletes nothing
awm transcripts prune --days 180 --dry-run false # deletes
```

There is no default retention. Deleting needs a number somebody chose.

## When a sweep reports failures

`failed` lists the sessions the sweep could not move, with the error. A sweep
continues past a failure by design: one unreadable file must not stop the other
several hundred. Fix the cause and run the sweep again — it is idempotent, since
an already-archived session is no longer in the live tree.

## Recovering a session

```bash
awm transcripts search --pattern 'some phrase'   # find it in the archive
awm transcripts restore --project <dir> --session <id>
```

`restore` refuses when a live copy exists rather than overwriting it.

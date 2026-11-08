# Installing the `transcripts` plugin

## Purpose & Contents

This file covers what transcripts needs, how its daily sweep runs, and what to do when a sweep reports failures. The verbs and their parameters come from `awm transcripts --help` and `transcripts(verb="describe")`.

## What it does

transcripts sweeps Claude Code session logs out of `~/.claude/projects` into a gzipped mirror at `~/.claude/projects-archive`, restores them, and prunes the archive. It also serves the opt-in live search source `transcripts`.

The unit is the session: `<project>/<sessionId>.jsonl` and `<project>/<sessionId>/` move together. **CAUTION** A project directory also holds a `memory/` folder at the depth of a session sidecar. It holds the auto-memory. `layout::RESERVED` excludes it, and any code that walks the tree must go through `layout::sessions`.

## Requirements

- Read and write access to `~/.claude/projects` and `~/.claude/projects-archive`.
- `harness-cc` on the node. A sweep asks it which sessions a Claude Code process still holds, and refuses to run when it cannot answer.

transcripts is a core plugin: the deploy places its static binary at `<AWM_DIR>/bin/transcripts`. It has no install step.

## The daily sweep

The plugin sweeps sessions older than 7 days at about 03:30 local time. When it starts more than 26 hours after its last timer sweep, it sweeps at once to catch up. A node that has never had a timer sweep waits for 03:30. The sweep runs at idle IO priority and nice 19, and its run row says `trigger = timer`.

    awm transcripts runs                 # what the last sweeps moved

**CAUTION** A first sweep after a long gap moves a large backlog. Run `awm transcripts sweep --dry-run` first when that matters.

Each archived file is decompressed and compared with its source before any live file of that session is deleted. New archive files are 0600 in 0700 directories, as the live files are.

## Pruning

`prune` is the only operation here that destroys data. It is CLI and HTTP only, never on the MCP surface, and it dry-runs unless told otherwise.

    awm transcripts prune --days 180                 # reports, deletes nothing
    awm transcripts prune --days 180 --dry-run false # deletes

There is no default retention. Deleting needs a number somebody chose.

## When a sweep reports failures

`failed` lists the sessions the sweep could not move, with the error. A failed session keeps every live file. The sweep continues past a failure, so one unreadable file does not stop the rest. Fix the cause and run the sweep again. It is idempotent, because an archived session is no longer in the live tree.

## Recovering a session

    awm transcripts search --pattern 'some phrase'   # find it in the archive
    awm transcripts restore --project <dir> --session <id>

`restore` refuses when a live copy exists rather than overwriting it.

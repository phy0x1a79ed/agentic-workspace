# agentic-commons

## Purpose & Contents

This repository is the workspace of the seats: the agents that talk with people through Slack and Discord on Tony's behalf. It holds the seat SOP, data the seats share, and one folder per person. It is apart from awm's `agentic-workspace`, metasmith and every other project (`users.md` R128).

- `docs/` — the seat SOP and other docs that apply to every seat.
- `data/` — DVC-tracked data that every seat shares. Track a chunk with `dvc add data/<chunk>`.
- `seats/<person>/` — one person's seat folder. Git ignores it here. It is a local git repository of its own, with DVC for its data and no remote.
- `scripts/` — `new-seat.sh` makes a seat folder.

## Who writes where

Only a person's own seat writes in `seats/<person>/`. Only the main seat writes outside `seats/`. So merges between nodes never collide.

## Backups

A lab member's seat folder is backed up by the nightly Globus backup of the node that holds it. A seat for someone outside the lab has no backup. Tell such a person that their files live only on that node.

## Remotes

The repository lives on Tony's GitHub, with a private fork on the Hallam lab GitHub. A seat folder has no remote.

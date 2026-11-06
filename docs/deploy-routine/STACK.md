# Deploy routine rework

Goal: a standard protocol that brings changes live: agents land their own work through a deterministic batch, refresh every node by a script, and spawn no agent for a failure. Tony drives the design over Remote Control.

Decisions: journal posts tagged `decision` in awm/deterministic-refresh. The starting decision is journal 5662544f (overrides routines.md R32).

## Current

land-script — scripts/land.sh: submit, wait, batch, bounce.

## Waiting on the user

- skills: review the land and merge skill drafts (~/.claude/skills/land, ~/.claude/skills/merge).

## Preferences

- 2026-11-06: no broadcasts. No session is warned before or after a restart.
- 2026-11-06: skills follow write-docs, without a Purpose & Contents section. Models: debrief and dfs-interview (concise, instructional, utilitarian, structured).
- 2026-11-06: feature agents test efficiently: unit and component smoke tests, no rehearsals.

## Stack

- land-script — the deterministic batch the skills call.
  - State: the WIP commit bbdab778 holds scripts/node-refresh.sh (sync, deploy, health, result per sha, transient unit) and engine changes (kind `detached`, resumable rollout). Reuse node-refresh.sh for the rollout step. The engine changes become moot if land.sh replaces routine deploy.
- retire-deploy-service — what happens to `routine deploy`, its fixers, and spec R19–R27, R47–R48.
- project-refresh — per-project refresh config, such as stock's.

## Workers

- hot-reload/interview — job 34faf88e, scope awm/hot-reload, own card. Restores hot reload of awm and its plugins; interviews Tony directly.

## Closed

- land-protocol — journal 76d838aa (feature agents land, promoted merger, no spawned fixers, no broadcasts).
- hot-swap — handed to hot-reload/interview (job 34faf88e).

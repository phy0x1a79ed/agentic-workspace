# Deploy routine rework

Goal: a standard protocol that brings changes live: agents land their own work through a deterministic batch, refresh every node by a script, and spawn no agent for a failure. Tony drove the design over Remote Control.

Decisions: journal posts tagged `decision` in awm/deterministic-refresh. The starting decision is journal 5662544f (overrides routines.md R32).

## Current

None. The discussion closed on 2026-11-06.

## Preferences

- 2026-11-06: no broadcasts. No session is warned before or after a restart.
- 2026-11-06: skills follow write-docs, without a Purpose & Contents section. Models: debrief and dfs-interview.
- 2026-11-06: feature agents test efficiently: unit and component smoke tests, no rehearsals.
- 2026-11-06: operator notices go to unimatrix0#notifications. Tony's DM is for conversation.

## Stack

- mizar-push — mizar pushes release into every node's live checkout. Decided ("mizar push makes the most sense to me"), not built.
- project-refresh — stock's release deploy.toml sets `[refresh] kind = "none"`, which the parser now refuses. Remove the line in stock's next landing.

## Closed

- land-protocol — journal 76d838aa.
- hot-swap — handed to hot-reload/interview (job 34faf88e).
- land-script, retire-deploy-service — journal e003ea63.
- routine-failures — journal ca8f611d.
- reinstall — journal 409bf913. Why pip at all: plugin-redux/interview (job d05a9018).

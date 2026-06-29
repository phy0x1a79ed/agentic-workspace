# graphify self-improvement loop — charter + protocol

This file is the **durable brain** of an autonomous `/loop` that improves the
graphify service. Context gets summarized between iterations; this file does
not. Read it (and `BACKLOG.md`) at the **start of every iteration** before doing
anything else. They are the source of truth for what's done, what's next, and
when to stop.

## Why this loop exists

graphify is a tool built **for an awm agent** (me) to navigate awm's own
architecture during development. The goal of the loop is to make it actually
trustworthy and useful for that consumer — not feature-complete in the
abstract. "Useful to the agent doing awm dev" is the north star for every
judgment call.

## Autonomy boundary (hard rules — do not cross)

- **Branch**: commit every increment to `feat/svc-graphify`. Small, coherent
  commits — one per increment.
- **Dogfood target**: live-test by shadowing onto the **feat-gamebot sandbox at
  `:7871`**. Never prod (`:7819`).
- **Promotion is human-gated**: do NOT merge to `dev` or `release`. When the
  backlog's definition-of-happy is met, stop and report; the human runs
  dev→release.
- **AST-only invariant is sacred**: a build must never make a paid LLM call.
  Keep `.graphifyignore` doc-exclusion + `_LLM_KEY_VARS` stripping intact. Any
  change touching the build path must preserve "build with no API key works".
- **awm data rules**: raw data immutable; write outputs under `.awm/data/`;
  never hand-edit `.awm/history.md` or `.awm/artifacts.md`.

## Per-iteration recipe

1. **Orient.** Read `LOOP.md` + `BACKLOG.md`. Pick the highest-priority
   unchecked backlog item whose prerequisites are met.
2. **Implement.** Make the smallest change that delivers that item. Keep the
   runner/​adapter/​manifest in parity (handler name ↔ tool name ↔ manifest).
3. **Unit-test.** `awm/gateway/scripts/run-tests.sh graphify` must be green.
   Add/extend tests for new behaviour (binary stays mocked in unit tests).
4. **Dogfood live on :7871.** Shadow the service onto the feat-gamebot hub and
   drive the real RPCs against the real binary (recipe below). A change isn't
   "done" until it's been exercised live at least once.
5. **Commit** to `feat/svc-graphify` with a focused message. Tick the backlog
   item, append a one-line dated note to the "Iteration log" at the bottom of
   `BACKLOG.md`.
6. **Reassess + schedule.** If definition-of-happy met → stop & report. Else
   schedule the next wake-up and continue.

## Live dogfood recipe (:7871 feat-gamebot)

Bring the sandbox up if it's down (it was down at loop start):

    # is it up?
    curl -fsS http://127.0.0.1:7871/hub/services >/dev/null && echo UP || echo DOWN

Bringing a sandbox hub up is via its worktree at
`/home/tony/agentic_workspace/projects/awm/feat-gamebot` (`awm dev start` there,
or the feat-gamebot run.sh). Confirm the hub answers before shadowing.

Shadow + drive (preferred, no install):

    cd awm/services/graphify
    AWM_PORT=7871 awm dev shadow awm/services/graphify   # overlay onto :7871
    # then drive via the hub:
    curl -s -XPOST http://127.0.0.1:7871/svc/graphify/fn/status -d '{}'
    curl -s -XPOST http://127.0.0.1:7871/svc/graphify/fn/build -d '{}'

**Footguns (learned, do not rediscover):**
- *No-base journaled respawn loop*: shadowing onto a hub that has **no base**
  for graphify self-registers a JOURNALED base that respawns on manual kill.
  Tear down cleanly with `AWM_PORT=7871 awm services stop graphify` (drops the
  journal first), never a bare kill + DELETE. (memory:
  awm_shadow_service_no_base_journaled)
- *Overlay name collision*: two shadows of the same service on one hub collide
  on the default `<svc>-shadow` overlay; pass a custom overlay name if needed.
- *GRAPHIFY_BIN in dev*: the dev branch of run.sh runs under `mamba run -n awm`
  where the graphify binary isn't on PATH — this is iteration #1's fix. Until
  it's fixed, shadow builds fail at `graphify_bin()`.
- *RPC timeout*: build can take seconds; the manifest already sets per-fn
  timeouts (build 600). Don't let a slow build 504.

## Stuck-detection / step-back (do not grind)

Stop the loop and report to the human — instead of iterating again — if ANY:
- The same approach has failed **2+ iterations** in a row (rabbit hole).
- Live dogfood on :7871 can't be made to work after a genuine attempt (e.g.
  sandbox won't come up) AND the blocker isn't something I can fix in-scope.
- A change would require crossing the autonomy boundary (merge, prod, paid LLM).
- The backlog is empty of Tier-1/Tier-2 items and remaining work is judgment
  calls the human should weigh in on.

When stopping: write the state to `BACKLOG.md`'s iteration log, summarize what's
done / what's left / why stopped, and hand back. Stopping with a clear report is
a success, not a failure.

## Definition of happy → see BACKLOG.md "Definition of happy".

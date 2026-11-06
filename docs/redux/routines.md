# Routines

## Purpose & Contents

This doc holds awm's registered routines: what a routine is, what triggers it, and what it does on failure. It also holds the routines designed so far: `deploy`, which takes a change from a scope branch to the running fleet, the full test suite, and public releases. Thread 1m produced it.

## Requirements

### Routines

- **R34.** awm keeps a set of registered routines. A routine makes a recurring process deterministic. A timer or an agent triggers it. Tony: "we can maybe have a series of registerable automations that makes these things deterministic. automations can be trigged by a timer or by agents."
  - A routine tests its conditions, then dispatches agents when one holds. The agents and tasks can run on remote peers. Tony: "On a timer do this and on any of these conditions, dispatch agents [...] agents and tasks can be on remote peers."
  - `deploy`, the full test suite and a public release are each a routine. Tony: "We can now use this abstraction to also describe  deployments."
- **R36.** A routine is defined by its project. Tony: "keeping the scripts with the parent project works [...] this encourages repos for them which adds version tracking which is encouraging good behaviour."
  - It has a name.
  - It runs on the node where it is installed. Its definition names no node.
  - It has triggers: a timer, a call, or both. Tony runs one through a call, the same as an agent does. Tony: "by hand = agent call."
  - It has deterministic steps, a failure that dispatches a session, and a report of one mark (R27).
  - Its definition lives in its project's repo. Work with no project gets a new repo (R4).
- **R47.** A failed run of any routine starts an agent to fix the error. It does not wait for Tony. Landing is not a routine: its failures go to the merger (R23). Tony: "the expected error handling for routines is to start an agent to address and fix the error" (2026-10-31).
  - The `routine` service starts it through the same API that the MCP tool calls.
  - Lineage traces through the run. The run records the session that triggered it. The fixing agent records the run as its parent (R13). Tony: "parent should somehow trace through agent -> deploy routine -> fail -> error handler agent" (2026-10-31).
- **R48.** A routine declares the peer that its fixing agent runs on. The routine itself still runs where it is installed (R36). When the fixing agent cannot start, the service falls back in order. Tony: "routines should also declare the peer that the error handler agent runs on. if peer not reachable -> discord DM notice (crucial failures should all attmpt to notify me through discord) -> finally just give up and log locally" (2026-10-31).
  1. Start the fixing agent on the declared peer.
  2. When that peer is unreachable, send Tony a notice in his Discord DM.
  3. When the notice fails too, log the failure locally and stop.
  - Every crucial failure tries the Discord DM notice. This matches its use for urgent notices today (precedence `40040e7a1ec36442`).
- **R37.** The service is an MCP service. `trigger` and `cancel` run and stop a routine. `schedule` and `unschedule` set and clear its timer. Tony: "awm <the service> trigger/cancel (manual trigger), or schedule/unschedule for timers."
- **R38.** systemd timers drive the timed triggers. Tony: "Good to use established robust mechanisms where possible." A timer fires by calling `trigger`. The service keeps the record of each run, allows one live run per routine, and watches each run to its verdict.
- **R39.** The service is named `routine`. Each registered process is a routine. Tony: "this is fine, I will use awm routine to clarify and context should be sufficient." Claude Code also calls its scheduled cloud agents routines. Write `awm routine` wherever the context leaves doubt.

### Deploy

- **R19.** One aggregator lands changes. It removes redundant and unrelated test runs. It stops deploys from stacking and racing. Tony: "right now, agents run redundant and unrelated tests repeatidly. deployments can also stack and race. Lets talk continuous integration systems, preferrably deterministic but there can be (and might necessarily require agentic elements)" (2026-10-31).
- **R20.** An implementation session tests its feature in isolation, then calls `deploy`. A deterministic system reviews each call. It spawns an agent only when one is needed. Tony: "implemenation sessions (standard sessions) should prepare for deploy and submit to teh service using some for of contract. essentially a pull request. A deterministic system reviews and spawns an integration agent if needed."
- **R21.** The service batches submissions that arrive together, and runs the shared checks once per batch, not once per submission. A batch has no size cap. Tony: "I want to prevent running abunch of the same checks like the test suite consecuteively each time for each merge" and "Lets have no cap."
- **R23.** A batch has a merger: the first feature agent to submit while no batch runs. The merger runs the batch and fixes its integration faults. It returns a branch's own fault to that branch's agent. No agent is started for a failure. Tony: "maybe it would make sense to actually promote a feature agent into a merger that lands its work plus those in a batch" and "the integration agent fixes integration issues" (2026-11-06).
- **R24.** `deploy` is one mostly deterministic core function, shared by every project. Its contract is a function signature, not a document. Tony: "I was after something closer to a function signature."
  1. Calling it places the current scope in the queue for an automated merge, feature to `dev` to `release`.
  2. `dev` is the staging branch. Integration is tested and verified there.
  3. Its end state: the feature branch's changes are in `release`, and every live process built on `release` is refreshed.
  4. Errors go to agents, as R23 describes.
- **R25.** Each project supplies a refresh hook. The hook refreshes that project's live processes after `release` moves. Tony gave three shapes:
  - Restart a service. For example, Trilium restarts for a core change.
  - Place an update task on the agentic queue, so that an agent coordinates a restart that disrupts the least. awm no longer does: it refreshes by script (R32).
  - Hot-update only what changed, in a project that supports it. For example, single awm services.
- **R26.** The signature of the land queue, `scripts/land.sh`. The skills `ci/land` and `ci/merge` carry the protocol.
  - `submit` takes the current scope implicitly, a merge brief and the restart list (R32). It pins the scope's head commit at call time. Later work needs a new call.
  - It returns a ticket id at once. Tony: "right, deploy submits to queue so it should return immediately..." (2026-11-01).
  - `wait` returns the verdict, `landed` or `bounced`, or names the caller the merger.
  - The feature agent proves the behavior it implemented, and only that. Tony: "agents are implementing a feature and they shuold be fairly confident that it performs to spec, but while being efficient with what they test" (2026-11-06).
- **R27.** The batch runs these deterministic steps. Any failure stops the batch and keeps its state for the merger (R23).
  1. **Pin.** Confirm that each pinned commit exists. The worktrees share one bare repo, so no push is needed.
  2. **Merge.** Merge the batch onto `dev`.
  3. **Pins.** Run the project's pin refresh in the batch's tree. It rebuilds each stale built-file pin into the landing node's artifact store. The engine commits the changed pins as `deploy: refresh pins`, so the next steps cover that commit. A failed build stops the batch before promote. The step has its own timeout, apart from the check's.
  4. **Check.** Run the project's smoke checks on `dev`. Smoke checks are fast. They tell that something is wrong, not what is wrong. A run longer than 1 minute is a failure of its own. Tony: "the tests for step 3 should be limited to at most 1 min and if this is exceeded, this is another error that spawns an integration agent."
  5. **Promote.** Fast-forward `release` to `dev`.
  6. **Refresh.** Run the project's refresh on each node (R31, R32). A failed refresh stops the rollout for the merger, and does not call Tony. Tony: "Calling me is not really productive with no agent to talk to."
  7. **Report.** Give each submitter one verdict: `landed`, or `bounced` with the reason. Tony: "maybe as small as a check box."
- **R29.** Only `deploy` writes `dev` and `release`. Agents never merge or push into either. Instructions carry the rule, with no hook to enforce it. A `reference-transaction` hook in each bare repo is the next step if agents break the rule. Tony: "lets just pick a and escalate if there are issues."
- **R31.** The fleet route. Tony: "this all looks good to me" (2026-10-31).
  - One `deploy` service per project runs on the node that holds the project's canonical repo. awm's canonical repo moves to mizar, the homebase. GitHub keeps a backup, which each deploy updates (`install.md` R114). Tony: "Mizar is the canonical home of AWM. But we should also try to keep the remote on GitHub up to date as a backup." (2026-11-04).
  - A session on another node pushes its feature branch to the canonical repo before it calls `deploy`.
  - awm work happens on mizar. altair runs awm but does no awm work, so it never pushes to the canonical repo. mizar sends each `release` to altair over its edge, or through GitHub, and altair's refresh hook takes it from there. Tony: "awm work happend on mizar. mizar can then push changes to altair directly or through github. altair shouldnt work on awm" (2026-11-04).
  - Home refreshes each other node over ssh with the project's refresh (R32). The node's checkout takes `release` first.
  - Nodes refresh one at a time, homebase first. A failed node stops the rollout before the remaining nodes, for the merger (R27).
  - How nodes reach each other belongs to thread 1g.
- **R32.** A node refreshes by a script, not by an agent: it moves the checkout, runs the deploy, checks every enabled service and records the result. It runs apart from the gateway, so an awm restart cannot cut it off. It restarts what the landing agents name. No session is warned before or after a restart. The merger supervises the rollout (R23). Tony: "we should change the deploy protocol to be deterministic with only agentic error handling", "we shouldnt do disruptive broadcasts. do not warn. most updates do not impact work" and "make determinisitic as possible, but need to have a supervisor" (2026-11-06). Hot-reload decision 133ab366: "calling agent decides what restarts, even if disagrees with file changes".
- **R33.** awm's plugins other than the core plugins move into projects of their own (`install.md` R115 item 4). The core plugins stay in the awm repo. Each plugin project has its own refresh hook, apart from awm's. A plugin then hot-updates without touching the core awm process. Tony: "for hot updates, this is actually an infrastructure issues. we need a series of projects to host the plugins for awm (needs migration)." Thread 1h designs the plugins themselves.
  1. engcyc's project lives on altair, because it is work. Its deploy runs in reverse, started from shogun. Every other optional plugin project lives on mizar. Tony: "I mainly just want engcyc on altair since it is work related. deployment can be reversed, started from shogun. everything else on mizar is fine" (2026-11-04).

### The full test suite

- **R30.** The full test suite runs outside `deploy`, as a timer-triggered routine (R34). Tony: "This is a separate feature."

### Public releases

- **R28.** A public release is not a feature deployment. Examples are a metasmith build and its upload to conda. Tony: "I also want to differentiate public releases (like metasmith uploads and builds to conda, etc) from these feature deployments."
- **R35.** The metasmith public release is a routine registered on altair. Tony triggers it with a call for now. Tony: "for things like metasmith, the public release can be a registered automation on altair and we trigger that manually for now."

## Decisions

- 2026-11-06: Agents land their own work through the land queue, and a promoted merger supervises each batch. The deploy service, its spawned fixers and its supervised refresh sessions are retired. They started a chain of fixers on 2026-11-06, each failing at a refresh session that could not start. Decision journals 5662544f and 76d838aa.
- 2026-11-01: R47 needs no loop guard. A fix chain stays linear and runs one fixer at a time, so a fixer that fails again starts the next fixer. Tony: "the worse that it ever gets is agent -> deploy routine -> fixer agent -> routine -> agent. a linear chain. at most 1 top level agent is running. There is no runaway effect where 1 becomes 2 becomes 4, etc"
- 2026-10-31: Sessions submit to a merge queue rather than land under a lock. Tony: "b makes sense, but I think  functionally we will end up having both." The service still serializes its own merges and deploys.

## Gap from today

Status at the wave-2 gather (`feat/redux` 16da3979, 2026-11-04). `docs/redux-review/build-survey.md` § 3 holds the row for each requirement.

- **R31.** The `[fleet]` config, the GitHub backup push and the altair push are built. Canonical moves from altair to mizar at T27 (`install.md` R114).
- **R33.** engcyc, rlm-factorio, zotero and claude-science carry a refresh hook. The cloud role `plugin_homes` splits them into their own projects at T27, and their awm folders go in the same step.
- **R28, R35.** The metasmith routine exists only as a draft in `drafts/redux-routines-plan.md`. R35 waits for a T27 worker on altair, where the metasmith repo lives.

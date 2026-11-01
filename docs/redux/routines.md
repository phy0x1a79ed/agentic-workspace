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
- **R47.** A failed run of any routine starts an agent to fix the error. It does not wait for Tony. This extends R23 from `deploy` to every routine. Tony: "the expected error handling for routines is to start an agent to address and fix the error" (2026-10-31).
  - The `routine` service starts it through the same API that the MCP tool calls. The started session records the routine run as its parent (R13).
- **R37.** The service is an MCP service. `trigger` and `cancel` run and stop a routine. `schedule` and `unschedule` set and clear its timer. Tony: "awm <the service> trigger/cancel (manual trigger), or schedule/unschedule for timers."
- **R38.** systemd timers drive the timed triggers. Tony: "Good to use established robust mechanisms where possible." A timer fires by calling `trigger`. The service keeps the record of each run, allows one live run per routine, and watches each run to its verdict.
- **R39.** The service is named `routine`. Each registered process is a routine. Tony: "this is fine, I will use awm routine to clarify and context should be sufficient." Claude Code also calls its scheduled cloud agents routines. Write `awm routine` wherever the context leaves doubt.

### Deploy

- **R19.** One aggregator lands changes. It removes redundant and unrelated test runs. It stops deploys from stacking and racing. Tony: "right now, agents run redundant and unrelated tests repeatidly. deployments can also stack and race. Lets talk continuous integration systems, preferrably deterministic but there can be (and might necessarily require agentic elements)" (2026-10-31).
- **R20.** An implementation session tests its feature in isolation, then calls `deploy`. A deterministic system reviews each call. It spawns an agent only when one is needed. Tony: "implemenation sessions (standard sessions) should prepare for deploy and submit to teh service using some for of contract. essentially a pull request. A deterministic system reviews and spawns an integration agent if needed."
- **R21.** The service batches submissions that arrive together, and runs the shared checks once per batch, not once per submission. A batch has no size cap. Tony: "I want to prevent running abunch of the same checks like the test suite consecuteively each time for each merge" and "Lets have no cap."
- **R22.** Coordinating sessions share a reserved batch. Tony: "there should also be a way to specify batches for agetns that are coordinating."
  1. One agent coordinates the work between the sessions.
  2. Each session reserves the batch first, under one shared name. A name is unique among live reservations.
  3. The batch goes through when every reservation is submitted or cancelled.
  4. Submissions without a reservation keep flowing in ordinary batches meanwhile.
  5. The coordinator makes the reservations before it starts the sessions. Tony: "the coordinator reserves deployments before even starting the agents."
  6. A reservation belongs to its session, not to its scope. When the session ends without a submission, the service cancels its reservation and tells the coordinator. A later session in the same scope never joins the stale batch. Its work lands in another batch.
- **R23.** A failed batch dispatches an implementation session, which works like any feature session. It takes the batch's work, fixes the integration and resubmits it as one combined submission. Later batches keep landing meanwhile. The service does not split a failed batch to find one cause. Tony: "I dont think isolating the failing merge will be useful. it will likely be an integration issue" and "failure dispatchs an implenetation agent [...] which acts just like a normal feature with deploy, only it takes the previous work from the batch, fixes the integration, then resubmits a combined (and fixed) deploy."
- **R24.** `deploy` is one mostly deterministic core function, shared by every project. Its contract is a function signature, not a document. Tony: "I was after something closer to a function signature."
  1. Calling it places the current scope in the queue for an automated merge, feature to `dev` to `release`.
  2. `dev` is the staging branch. Integration is tested and verified there.
  3. Its end state: the feature branch's changes are in `release`, and every live process built on `release` is refreshed.
  4. Errors go to agents, as R23 describes.
- **R25.** Each project supplies a refresh hook. The hook refreshes that project's live processes after `release` moves. Tony gave three shapes:
  - Restart a service. For example, Trilium restarts for a core change.
  - Place an update task on the agentic queue, so that an agent coordinates a restart that disrupts the least. For example, awm.
  - Hot-update only what changed, in a project that supports it. For example, single awm services.
- **R26.** The signature of `deploy`. Tony: "this looks good" (2026-10-31).
  - It takes the current scope implicitly, and no other required argument. It takes no reservation name. The coordinator attaches a reservation when it starts the session, and `deploy` finds it there. Tony: "agents failing to provide the optional reservation name will be a source of error."
  - It pins the scope's head commit at call time. Later work needs a new call.
  - It returns a ticket id at once. The verdict posts to the session's card and journal.
  - The calling session retires when nothing else is pending (R12). A coordinator that needs the verdict waits on the ticket. Thread 1c designs that wait.
- **R27.** `deploy` runs these deterministic steps on a batch. Any failure dispatches an implementation session (R23).
  1. **Pin.** Confirm that each pinned commit exists. The worktrees share one bare repo, so no push is needed.
  2. **Merge.** Merge the batch onto `dev`.
  3. **Check.** Run the project's smoke checks on `dev`. Smoke checks are fast. They tell that something is wrong, not what is wrong. A run longer than 1 minute is a failure of its own. Tony: "the tests for step 3 should be limited to at most 1 min and if this is exceeded, this is another error that spawns an integration agent."
  4. **Promote.** Fast-forward `release` to `dev`.
  5. **Refresh.** Run the project's refresh hook (R25). A failed refresh dispatches a session too, and does not call Tony. Tony: "Calling me is not really productive with no agent to talk to."
  6. **Report.** Show each submitter one mark. A tick means the batch landed. A cross names the failed step (merge, check failure, check timeout, refresh) and links to the card of the dispatched session. Tony: "maybe as small as a check box."
- **R29.** Only `deploy` writes `dev` and `release`. Agents never merge or push into either. Instructions carry the rule, with no hook to enforce it. A `reference-transaction` hook in each bare repo is the next step if agents break the rule. Tony: "lets just pick a and escalate if there are issues."
- **R31.** The fleet route. Tony: "this all looks good to me" (2026-10-31).
  - One `deploy` service per project runs on the node that holds the project's canonical repo. awm's canonical repo moves to mizar, the homebase.
  - A session on another node pushes its feature branch to the canonical repo before it calls `deploy`.
  - Each other node refreshes itself through the project's refresh hook (R25). The hook fetches `release`, then restarts or queues a coordinated restart.
  - Nodes refresh one at a time, homebase first. A failed node dispatches a session (R27) and stops the rollout before the remaining nodes.
  - How nodes reach each other belongs to thread 1g.
- **R32.** A sensitive project such as awm restarts under supervision. On each node, a local agent coordinates with the other local agents and supervises the restart. Tony: "for sensitive things like awm, a local agent needs to coordinate with other local agents and supervise the restart."
- **R33.** awm's plugins move into projects of their own. Each peer installs some of them. Each plugin project has its own refresh hook, apart from awm's. A plugin then hot-updates without touching the core awm process. Tony: "for hot updates, this is actually an infrastructure issues. we need a series of projects to host the plugins for awm (needs migration)." Thread 1h designs the plugins themselves.

### The full test suite

- **R30.** The full test suite runs outside `deploy`, as a timer-triggered routine (R34). Tony: "This is a separate feature."

### Public releases

- **R28.** A public release is not a feature deployment. Examples are a metasmith build and its upload to conda. Tony: "I also want to differentiate public releases (like metasmith uploads and builds to conda, etc) from these feature deployments."
- **R35.** The metasmith public release is a routine registered on altair. Tony triggers it with a call for now. Tony: "for things like metasmith, the public release can be a registered automation on altair and we trigger that manually for now."

## Decisions

- 2026-10-31: Sessions submit to a merge queue rather than land under a lock. Tony: "b makes sense, but I think  functionally we will end up having both." The service still serializes its own merges and deploys.
- 2026-10-31: A batch cannot be edited to take a retry for a session that died. A new session lands the work in another batch. Tony: "under the current architecture, a new session will be able to handle this fine, just in another batch. acceptable."

## Gap from today

Surveyed read only on 2026-10-31, from the `feat/redux` source.

- **No serialization.** No lock, queue or lease guards a deploy, a merge into `dev` or a merge into `release`. `awm deploy` and `scripts/promote.sh` take no lock. Two runs at once can reset the tree under each other's install, overlap restarts, and race the manifest write. Journals record it happening: concurrent deploys restarted capella mid-heal (`39cdefeb`), `dev` moved under a session mid-merge (`f134b381`), and root resets destroyed work (`e91cd30d`, `cfb2e43c`).
- **The canonical repo is on altair.** awm's canonical bare repo is `altair:projects/awm/.bare`. Every node resets onto altair's `release`.
- **No fleet route in code.** `awm deploy` acts on the local node only. It installs, builds, restarts the unit and polls `/hub/services` (`gateway/cli.py:301-458`). `scripts/promote.sh` merges, tests, promotes and deploys, but only on altair, with sirius optional. No root doc names it. Agents update mizar, alcor and deneb by hand.
- **No test selection.** `gateway/scripts/run-tests.sh` runs pytest per dist, by name, with the cache off. Nothing maps changed files to dists. Nothing records which sha passed which dist. Each agent chooses suites by hand. ARCHITECTURE.md says run the full suite. The global CLAUDE.md says run the tests the change touches.
- **No smoke tier.** The gateway's `pyproject.toml` defines a `smoke` marker that the runner never selects. No suite has a time budget.
- **Known failures are rediscovered.** Dists left out of the runner's list never ran (`424240d6`, `ae72fa98`). Suites that already fail on `release` make each agent re-check them by hand (`ca55d138`, `97eadaa3`).
- **No CI beyond that.** No git hooks, no hosted CI, no watcher. After a deploy, only the gateway's service poll and `promote.sh`'s sha check run. Nothing rolls back. Nothing records who deployed what, when.
- **Restarts ignore live work.** A deploy restart kills tether sessions and ssh ControlMasters (`d16ecbd1`, `549faf6a`, `cb95f74f`).
- **Plugins live in the core.** awm's services are dists inside the awm repo. A deploy restarts the one gateway unit that hosts them all.
- **Public releases are sessions.** A metasmith release runs in a `release` scope by hand. `dev.sh` builds the wheel, the Docker image, the Apptainer image and the conda package. A session uploads them to quay.io and anaconda.org, or leaves the upload commands for Tony (journals `cbc0bd7c`, `8d9360cc`, `667128c5`). One release missed its conda publish.
- **Three schedulers exist.** The `events` service fires cron and interval schedules as `schedule.tick` events. The `dvc` service runs its own cadence engine, with a vendored copy of `events/cron.py`. `transcripts` uses a systemd timer. None of them dispatches agents on a condition, and no registry lists routines.
- **dvc left systemd once.** `dvc` moved its backups off a systemd timer on 2026-08-13 (`eb739429`). The timer left only a log line, guarded only one of three submitting processes, and watched no run to its verdict. R38 keeps systemd for the firing and keeps those three duties in the service.
- **Other projects.** `arbutus-infra` runs Terraform and Ansible with `make check`. `cloud` deploys deneb and sirius with rsync scripts. `self-improvement` copies prompt docs with a `--check` drift mode. `game-bot` has no protocol.
- **No batching.** The `scatter-gather` skill gathers several branches into a hub branch, but runs no tests on the result.
- **No refresh hook.** `awm deploy` restarts the whole gateway unit. No project declares how its live processes refresh.

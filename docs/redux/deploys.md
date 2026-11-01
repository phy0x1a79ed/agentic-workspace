# Deploys

## Purpose & Contents

This doc holds how a change gets from a scope branch to the running fleet: the tests it passes, the merges, and the deploy to each node. Thread 1m produced it.

## Requirements

- **R19.** One aggregator lands changes. It removes redundant and unrelated test runs. It stops deploys from stacking and racing. Tony: "right now, agents run redundant and unrelated tests repeatidly. deployments can also stack and race. Lets talk continuous integration systems, preferrably deterministic but there can be (and might necessarily require agentic elements)" (2026-10-31).
- **R20.** An implementation session tests its feature in isolation, then calls `deploy`. A deterministic system reviews each call. It spawns an agent only when one is needed. Tony: "implemenation sessions (standard sessions) should prepare for deploy and submit to teh service using some for of contract. essentially a pull request. A deterministic system reviews and spawns an integration agent if needed."
- **R24.** `deploy` is one mostly deterministic core function, shared by every project. Its contract is a function signature, not a document. Tony: "I was after something closer to a function signature."
  1. Calling it places the current scope in the queue for an automated merge, feature to `dev` to `release`.
  2. `dev` is the staging branch. Integration is tested and verified there.
  3. Its end state: the feature branch's changes are in `release`, and every live process built on `release` is refreshed.
  4. Errors go to agents, as R23 describes.
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
- **R28.** A public release is not a feature deployment. Examples are a metasmith build and its upload to conda. Tony: "I also want to differentiate public releases (like metasmith uploads and builds to conda, etc) from these feature deployments."
- **R25.** Each project supplies a refresh hook. The hook refreshes that project's live processes after `release` moves. Tony gave three shapes:
  - Restart a service. For example, Trilium restarts for a core change.
  - Place an update task on the agentic queue, so that an agent coordinates a restart that disrupts the least. For example, awm.
  - Hot-update only what changed, in a project that supports it. For example, single awm services.
- **R21.** The service batches submissions that arrive together, and runs the shared checks once per batch, not once per submission. A batch has no size cap. Tony: "I want to prevent running abunch of the same checks like the test suite consecuteively each time for each merge" and "Lets have no cap."
- **R22.** Coordinating sessions share a reserved batch. Tony: "there should also be a way to specify batches for agetns that are coordinating."
  1. One agent coordinates the work between the sessions.
  2. Each session reserves the batch first, under one shared name. A name is unique among live reservations.
  3. The batch goes through when every reservation is submitted or cancelled.
  4. Submissions without a reservation keep flowing in ordinary batches meanwhile.
  5. The coordinator makes the reservations before it starts the sessions. Tony: "the coordinator reserves deployments before even starting the agents."
  6. A reservation belongs to its session, not to its scope. When the session ends without a submission, the service cancels its reservation and tells the coordinator. A later session in the same scope never joins the stale batch. Its work lands in another batch.
- **R23.** A failed batch dispatches an implementation session, which works like any feature session. It takes the batch's work, fixes the integration and resubmits it as one combined submission. Later batches keep landing meanwhile. The service does not split a failed batch to find one cause. Tony: "I dont think isolating the failing merge will be useful. it will likely be an integration issue" and "failure dispatchs an implenetation agent [...] which acts just like a normal feature with deploy, only it takes the previous work from the batch, fixes the integration, then resubmits a combined (and fixed) deploy."

## Decisions

- 2026-10-31: Sessions submit to a merge queue rather than land under a lock. Tony: "b makes sense, but I think  functionally we will end up having both." The service still serializes its own merges and deploys.
- 2026-10-31: A batch cannot be edited to take a retry for a session that died. A new session lands the work in another batch. Tony: "under the current architecture, a new session will be able to handle this fine, just in another batch. acceptable."

## Gap from today

Surveyed read only on 2026-10-31, from the `feat/redux` source.

- **No serialization.** No lock, queue or lease guards a deploy, a merge into `dev` or a merge into `release`. `awm deploy` and `scripts/promote.sh` take no lock. Two runs at once can reset the tree under each other's install, overlap restarts, and race the manifest write. Journals record it happening: concurrent deploys restarted capella mid-heal (`39cdefeb`), `dev` moved under a session mid-merge (`f134b381`), and root resets destroyed work (`e91cd30d`, `cfb2e43c`).
- **No fleet route in code.** `awm deploy` acts on the local node only. It installs, builds, restarts the unit and polls `/hub/services` (`gateway/cli.py:301-458`). `scripts/promote.sh` merges, tests, promotes and deploys, but only on altair, with sirius optional. No root doc names it. Agents update mizar, alcor and deneb by hand.
- **No test selection.** `gateway/scripts/run-tests.sh` runs pytest per dist, by name, with the cache off. Nothing maps changed files to dists. Nothing records which sha passed which dist. Each agent chooses suites by hand. ARCHITECTURE.md says run the full suite. The global CLAUDE.md says run the tests the change touches.
- **No smoke tier.** The gateway's `pyproject.toml` defines a `smoke` marker that the runner never selects. No suite has a time budget.
- **Known failures are rediscovered.** Dists left out of the runner's list never ran (`424240d6`, `ae72fa98`). Suites that already fail on `release` make each agent re-check them by hand (`ca55d138`, `97eadaa3`).
- **No CI beyond that.** No git hooks, no hosted CI, no watcher. After a deploy, only the gateway's service poll and `promote.sh`'s sha check run. Nothing rolls back. Nothing records who deployed what, when.
- **Restarts ignore live work.** A deploy restart kills tether sessions and ssh ControlMasters (`d16ecbd1`, `549faf6a`, `cb95f74f`).
- **Other projects.** `arbutus-infra` runs Terraform and Ansible with `make check`. `cloud` deploys deneb and sirius with rsync scripts. `self-improvement` copies prompt docs with a `--check` drift mode. `game-bot` has no protocol.
- **No batching.** The `scatter-gather` skill gathers several branches into a hub branch, but runs no tests on the result.
- **No refresh hook.** `awm deploy` restarts the whole gateway unit. No project declares how its live processes refresh.

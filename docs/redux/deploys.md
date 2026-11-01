# Deploys

## Purpose & Contents

This doc holds how a change gets from a scope branch to the running fleet: the tests it passes, the merges, and the deploy to each node. Thread 1m produced it.

## Requirements

- **R19.** One aggregator lands changes. It removes redundant and unrelated test runs. It stops deploys from stacking and racing. Tony: "right now, agents run redundant and unrelated tests repeatidly. deployments can also stack and race. Lets talk continuous integration systems, preferrably deterministic but there can be (and might necessarily require agentic elements)" (2026-10-31).
- Each project keeps its own deploy protocol. The aggregator runs it.
- **R20.** An implementation session prepares its change and submits it to the landing service under a contract, in effect a pull request. The session tests its feature in isolation before it submits. A deterministic system reviews each submission. It spawns an integration agent only when one is needed. Tony: "implemenation sessions (standard sessions) should prepare for deploy and submit to teh service using some for of contract. essentially a pull request. A deterministic system reviews and spawns an integration agent if needed."
- **R21.** The service batches submissions that arrive together, and runs the shared checks once per batch, not once per submission. A batch has no size cap. Tony: "I want to prevent running abunch of the same checks like the test suite consecuteively each time for each merge" and "Lets have no cap."
- **R22.** Coordinating sessions share a reserved batch. Tony: "there should also be a way to specify batches for agetns that are coordinating."
  1. One agent coordinates the work between the sessions.
  2. Each session reserves the batch first, under one shared name. A name is unique among live reservations.
  3. The batch goes through when every reservation is submitted or cancelled.
  4. Submissions without a reservation keep flowing in ordinary batches meanwhile.
- **R23.** A failed batch goes to an integration agent, dispatched like any other task. The batch then collapses into a single deployment. The service does not split a failed batch to find one cause. Tony: "I dont think isolating the failing merge will be useful. it will likely be an integration issue."

## Decisions

- 2026-10-31: Sessions submit to a merge queue rather than land under a lock. Tony: "b makes sense, but I think  functionally we will end up having both." The service still serializes its own merges and deploys.

## Gap from today

Surveyed read only on 2026-10-31, from the `feat/redux` source.

- **No serialization.** No lock, queue or lease guards a deploy, a merge into `dev` or a merge into `release`. `awm deploy` and `scripts/promote.sh` take no lock. Two runs at once can reset the tree under each other's install, overlap restarts, and race the manifest write. Journals record it happening: concurrent deploys restarted capella mid-heal (`39cdefeb`), `dev` moved under a session mid-merge (`f134b381`), and root resets destroyed work (`e91cd30d`, `cfb2e43c`).
- **No fleet route in code.** `awm deploy` acts on the local node only. It installs, builds, restarts the unit and polls `/hub/services` (`gateway/cli.py:301-458`). `scripts/promote.sh` merges, tests, promotes and deploys, but only on altair, with sirius optional. No root doc names it. Agents update mizar, alcor and deneb by hand.
- **No test selection.** `gateway/scripts/run-tests.sh` runs pytest per dist, by name, with the cache off. Nothing maps changed files to dists. Nothing records which sha passed which dist. Each agent chooses suites by hand. ARCHITECTURE.md says run the full suite. The global CLAUDE.md says run the tests the change touches.
- **Known failures are rediscovered.** Dists left out of the runner's list never ran (`424240d6`, `ae72fa98`). Suites that already fail on `release` make each agent re-check them by hand (`ca55d138`, `97eadaa3`).
- **No CI beyond that.** No git hooks, no hosted CI, no watcher. After a deploy, only the gateway's service poll and `promote.sh`'s sha check run. Nothing rolls back. Nothing records who deployed what, when.
- **Restarts ignore live work.** A deploy restart kills tether sessions and ssh ControlMasters (`d16ecbd1`, `549faf6a`, `cb95f74f`).
- **Other projects.** `arbutus-infra` runs Terraform and Ansible with `make check`. `cloud` deploys deneb and sirius with rsync scripts. `self-improvement` copies prompt docs with a `--check` drift mode. `game-bot` has no protocol.
- **No batching.** The `scatter-gather` skill gathers several branches into a hub branch, but runs no tests on the result.

# redux-walk: preferences

## Purpose

This file holds the preferences the user set for this discussion, each with its date. A decision that holds beyond this discussion goes to `precedence`.

## Preferences

- (2026-11-05) Build straight for the end state; do not spend work keeping things running in the interim. Tony: "Yeah, let's forget about making things work in the interim and just focus on getting the end state, like getting towards the end state." (2026-11-05)
- (2026-11-05) Deploy on many fronts at once and fix by patch. Reserve the dry-run, held-login and rollback care for changes that can lock a node out (sshd, firewall). Tony: "its that it has to be right before anything else can start / why cant we incrementally deploy to deneb and move on more fronts? / if something happens, cant we just deploy patches to fix?" (2026-11-05)
- (2026-11-05) Run unit tests for the code you touch. Skip the long runs: container suites, full multi-dist suites, dry runs and repeat runs. Ship features and test live. The held second login on sshd and firewall changes stays, because it is not a test. Tony: "agents are testing excessively. skip all tests / ship features and test live" and "unit tests are fine, but rt-container build has been running hour long tests multiple times" (2026-11-05)
- (2026-11-05) Build each component and stand it up, then check it live. Progress toward the end state comes in increments, each one live, not as one big tested cutover. Tony: "essentially, lets just build the components and stand them up / we can check functionality live / this prevents testing from the beginning / and we can make incremental progress towards end state" (2026-11-05)
- (2026-11-05) Work in passes. A worker writes a formal plan, makes one pass, and retires with a report. Whatever is left goes to a new pass that fills the gaps, so each front moves toward its end state in increments. Tony: "they will make a pass and should retire with a report / if anything is left doing, we will simply start another pass to gap fill / this will incrementally progress towards alcor endstate for taking over mira" (2026-11-05)
- (2026-11-05) One task per worker. A worker retires when its task is done. Start a new worker only for the next task, so workers never pile up. Each worker merges, releases and deploys its own change. Tony: "split. 1 task per worker. retire workers when done. dont keep pile-ing up workers" (2026-11-05)

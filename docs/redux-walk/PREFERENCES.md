# redux-walk: preferences

## Purpose

This file holds the preferences the user set for this discussion, each with its date. A decision that holds beyond this discussion goes to `precedence`.

## Preferences

- (2026-11-05) Build straight for the end state; do not spend work keeping things running in the interim. Tony: "Yeah, let's forget about making things work in the interim and just focus on getting the end state, like getting towards the end state." (2026-11-05)
- (2026-11-05) Deploy on many fronts at once and fix by patch. Reserve the dry-run, held-login and rollback care for changes that can lock a node out (sshd, firewall). Tony: "its that it has to be right before anything else can start / why cant we incrementally deploy to deneb and move on more fronts? / if something happens, cant we just deploy patches to fix?" (2026-11-05)
- (2026-11-05) Run unit tests for the code you touch. Skip the long runs: container suites, full multi-dist suites, dry runs and repeat runs. Ship features and test live. The held second login on sshd and firewall changes stays, because it is not a test. Tony: "agents are testing excessively. skip all tests / ship features and test live" and "unit tests are fine, but rt-container build has been running hour long tests multiple times" (2026-11-05)

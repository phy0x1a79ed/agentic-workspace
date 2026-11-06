# trust-users

## Purpose

This file holds the result of the review topic on how awm applies session trust.

## As built

- A failed session lookup reads as trusted with no scope, in both gateways (redux-harness e93907a0, redux-core 4ef3593b).
- The Rust gateway has no mode gate.

## Decisions

- 2026-11-04: A failed lookup runs as untrusted. Each session is recorded at its start, so a hand-started session stays trusted. Both gateways gate the mode from the same lookup (`fleet.md` R103 item 2). Settled by R103, which Tony set: "so trust level per session also needs to be set and tracked by awm so agents cant be coerced to elevate".

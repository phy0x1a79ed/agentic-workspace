# tooling

## Purpose & Contents

This file holds whether existing tools meet the spec, in whole or in part, and the shape of the gap left to design: gap-fill around a tool or a ground-up build.

## Survey

Two read-only surveys run on 2026-11-05: integrated engines scored against the spec, and disk-based exact substring indexes.

## Decisions

- 2026-11-05: Exact matching may bend to keep spaces when that lets an existing method serve it. The relaxed form is a case-insensitive phrase match on words, with punctuation as a word boundary: `p95-latency` matches `p95 latency`, but `p95latency` does not. Tony: "We can bend exact matching if existing methods keep spaces".

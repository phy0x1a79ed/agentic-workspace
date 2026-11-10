# Session ids and names: discussion stack

Goal: one canonical session id with named converters, one typed resolver, and names that are only names.
Decisions: docs/session-ids/DECISIONS.md

## Current

name-is-name

## Waiting on the user

- name-is-name: keep or drop the start/rename character rules; which name the resolver matches.

## Preferences

## Stack

- name-is-name: "a name is just a name". The /rc translation and display rule are already decided under ids-harmonize.
- stored-keys: state keyed on the short id or name (seats `job`, telemetry fallback, board cards) moves to the canonical id.

## Workers

## Closed

- ids-harmonize
- resume-short-id

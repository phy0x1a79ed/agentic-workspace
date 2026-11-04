# fleet-deploy

## Purpose

This file holds the result of the review topic on how awm deploys across the fleet: the canonical repo and how isolated sessions log in.

## As built

- origin is `altair:projects/awm/.bare`, and every node resets onto altair's `release`.

## Decisions

- 2026-11-04: mizar holds awm's canonical repo. GitHub is a backup that each deploy updates (`routines.md` R31, `install.md` R114). Tony: "Mizar is the canonical home of AWM. But we should also try to keep the remote on GitHub up to date as a backup."

# Port plan: dashboard

## Purpose & Contents

This file holds the approved port plan for dashboard, from task T4 of the ops group's plan (task CWPYAtyb), revised and approved on 2026-11-06. It names only what changes. The Python code and its manifests are the reference for everything the port keeps. The rules that hold for every ops port are in [README.md](README.md). Later changes go in the port commits and the journal.

## Move dashboard into core/plugins

Run `git mv awm/plugins/dashboard core/plugins/dashboard`. dashboard has no service and no state. Its pages call only the gateway's `config_contracts` and `config_set`. It lands after PLAN.md T2's page pipeline covers `core/plugins`.

Checks:
- The build lists the same three pages.
- The tarball holds only the new path.
- A page edit changes `pages_version`.
- On mizar and alcor, the pages return 200 with the old hashes, and the settings card saves.


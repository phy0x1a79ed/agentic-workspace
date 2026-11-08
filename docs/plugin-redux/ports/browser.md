# Port plan: browser

## Purpose & Contents

This file holds the approved port plan for browser, from the comms group's plan (task CWPYAtyb), approved by Tony on 2026-11-06. It names only what the Python does not make obvious. The Python at release 49232bf9 is the reference for everything kept. The rules every comms port follows are in `comms.md`.

## Plan

Replaces `awm/plugins/browser/` (493 lines). It runs on alcor as external unit `awm-plugin@browser`. The surface is unchanged: 12 verbs, the `cdp` allow-list, and the error shapes. Changes:
1. A seat belongs to `caller.session` plus node, at level `caller.trust`. A local caller with no session (`NO_SESSION_TRUST`, `caller.rs:30`) gets no seat, so no unkeyed caller shares Tony's profile.
2. It reconnects after a Chrome restart.
3. Seats persist in `seats.json`. A restart re-adopts them and closes only windows it recorded. The first start closes nothing.
4. Service tabs are matched by URL, so a restart adds no second set.
5. `seat_close` deletes the seat's copy of Tony's profile. A start reaps a copy only when no running seat container mounts it.

It finds Chrome through `cdp` in `browser.json`, the cloud role's container, unchanged. The `operations` need goes. `approval_request` stays a call. Tests: port `test_seats.py` against a fake Chrome, plus the five changes. The live check is `seat_open` from mizar and from alcor, a tab, a screenshot and close.

Python-era seat containers (`awm-browser-seat-*`) have no owner after the cutover, because the Python seats lived in memory. The inventory records them, and the land stops them. Their profile dirs stay for the reap rule.

Gotchas: it lands after operations' cloud-role drop-in for external units (X1). Stay under the unit's 128 MB cap.

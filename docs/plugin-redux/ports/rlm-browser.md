# Port plan: rlm-browser

## Purpose & Contents

This file holds the approved port plan for rlm-browser, from the comms group's plan (task CWPYAtyb), approved by Tony on 2026-11-06. It names only what the Python does not make obvious. The Python at release 49232bf9 is the reference for everything kept. The rules every comms port follows are in `comms.md`.

## Plan

Replaces `awm/services/rlm-browser/` (1376 lines) and moves its `docker/` image unchanged. It runs nowhere today (profile `gamebot`). The surface is unchanged. It reads `rlm-browser.db` v2 in place through awm-store. Following R102 item 6, it stays usable at every level but never as Tony:
- `headless` and `rendered` launch a fresh per-game profile.
- `attached` below trusted opens a guest context, and `release` disposes it.
- A forwarded attached `acquire` carries the original caller's level to the peer, so the guest window applies there too.
- A guest session's `cdp` takes browser's page allow-list. It refuses `__browser__`, `Target.*`, `Browser.*`, `Storage.*` and the cookie methods.

Tests: port `test_modes.py`, add guest-context tests, and add a real headless Chrome test when one is on `PATH`. The live check enables `gamebot` on mizar for one run.

Gotchas: it waits for awm-store. A re-attached docker session must remove its container by name.

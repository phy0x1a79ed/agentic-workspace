# mzio-signup — STACK

Goal: create Tony's mzio account, then clean up stale browser windows on alcor.
Decisions: ./DECISIONS.md. Moved into the awm scope browser-seat-reap on 2026-11-14.

## Current
seat-close-instructions

## Waiting on the user
- Where the close-your-seat rule lives (seat-close-instructions).

## Preferences
- 2026-11-13: Tony handles human steps over VNC.

## Stack
- alcor-browser-cleanup: "clean up browser windows on alcor when all done, suspect there are a lot just sitting there". Keep the stickers-picker seat; it was sent a close-when-done reminder. Close this session's seat at the end.
  - seat-close-instructions: "maybe remind them to close after (maybe this is the real solution, where to place these instructions? You were also not told to close the window when done)". Survey: nothing reaps a seat when its session retires, and `stop_if_idle` never stops Chrome while any seat is recorded.
- fullscreen-windows: "can we have all windows be full screen? and default open to fullscreen?"
- chrome-saved-login: "chrome should remember the username and password". Applies to the mzio login in alcor's tony profile.

## Workers

## Closed
- mzio-account
- productivity-browser-separation
- productivity-tab-leak

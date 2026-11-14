# mzio-signup — STACK

Goal: create Tony's mzio account, then clean up stale browser windows on alcor.
Decisions: ./DECISIONS.md. Moved into the awm scope browser-seat-reap on 2026-11-14.

## Current
seat-close-instructions

## Waiting on the user

## Preferences
- 2026-11-13: Tony handles human steps over VNC.

## Stack
- alcor-browser-cleanup: "clean up browser windows on alcor when all done, suspect there are a lot just sitting there". Keep the stickers-picker seat; it was sent a close-when-done reminder. Close this session's seat at the end.
  - seat-close-instructions: "maybe remind them to close after (maybe this is the real solution, where to place these instructions? You were also not told to close the window when done)". Decided 1+2: reap seats on session retire, plus a `when_done` line in the seat_open reply. Code on feat/browser-seat-reap, green on capella; next ship (plan T8, /home/tony/.claude/plans/1-and-2-please-cozy-dusk.md).
- fullscreen-windows: "can we have all windows be full screen? and default open to fullscreen?"
- chrome-saved-login: "chrome should remember the username and password". Applies to the mzio login in alcor's tony profile.

## Workers

## Closed
- mzio-account
- productivity-browser-separation
- productivity-tab-leak

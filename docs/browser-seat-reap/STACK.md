# mzio-signup — STACK

Goal: create Tony's mzio account, then clean up stale browser windows on alcor.
Decisions: ./DECISIONS.md. Moved into the awm scope browser-seat-reap on 2026-11-14.

## Current
reap-reach

## Waiting on the user
- reap-reach: which way to cover seats from nodes alcor cannot read.

## Preferences
- 2026-11-13: Tony handles human steps over VNC.

## Stack
- alcor-browser-cleanup: "clean up browser windows on alcor when all done, suspect there are a lot just sitting there". Stickers-picker's seat and this session's seat are closed.
  - seat-close-instructions: "maybe remind them to close after (maybe this is the real solution, where to place these instructions? You were also not told to close the window when done)". Decided 1+2: reap seats on session retire, plus a `when_done` line in the seat_open reply. Shipped in release 2e3b540d on 2026-11-14. Verified on alcor: when_done in the reply, the mizar session watch connected, every recorded seat belongs to a live session. No live reap observed yet.
    - reap-reach: alcor can read reflection only on mizar. Its altair edge is a service key with no tools (port.log: refused every 30s), and it has no edge to capella, lyra, deneb or shogun. Their seats never reap and stay open.
- fullscreen-windows: "can we have all windows be full screen? and default open to fullscreen?"
- chrome-saved-login: "chrome should remember the username and password". Applies to the mzio login in alcor's tony profile.

## Workers

## Closed
- mzio-account
- productivity-browser-separation
- productivity-tab-leak

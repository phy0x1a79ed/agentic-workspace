# mzio-signup — STACK

Goal: create Tony's mzio account, then clean up stale browser windows on alcor.
Decisions: ./DECISIONS.md. Moved into the awm scope browser-seat-reap on 2026-11-14.

## Current
chrome-on-demand

## Waiting on the user

## Preferences
- 2026-11-13: Tony handles human steps over VNC.

## Stack
- chrome-on-demand: a Chrome that stops loses every seat window, and nothing starts it again until a seat_open. Only seat_open, signin, viewer and the startup settle run ensure(), so cdp, navigate and seat_close on a recorded seat fail with 502 json/version. Seen twice: alcor's browser Docker restarted at 06:04 2026-11-14 (cause open), and the browser_chrome role drops awm-chrome on purpose when it rebuilds the image (2026-11-15 14:2x). At the next start, settle forgets the seats whose tabs are gone.

## Workers

## Closed
- chrome-saved-login
- fullscreen-windows
- alcor-browser-cleanup
- seat-close-instructions
- reap-reach
- mzio-account
- productivity-browser-separation
- productivity-tab-leak

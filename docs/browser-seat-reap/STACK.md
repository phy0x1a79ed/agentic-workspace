# mzio-signup — STACK

Goal: create Tony's mzio account, then clean up stale browser windows on alcor.
Decisions: ./DECISIONS.md. Moved into the awm scope browser-seat-reap on 2026-11-14.

## Current
chrome-saved-login

## Waiting on the user
- chrome-saved-login: Tony asked "Why cant they just pick the account?" Next: test whether an agent can pick the saved login (focus #username on auth.mzio.io in a tony seat, Input.dispatchKeyEvent ArrowDown then Enter, check #username filled). Chrome was down when tried.

## Preferences
- 2026-11-13: Tony handles human steps over VNC.

## Stack
- chrome-saved-login: "chrome should remember the username and password". The login IS saved: Google account password store (Default/Login Data For Account, 742 logins), signon_realm https://auth.mzio.io/, 6-char username, saved from the sign-up password form. Chrome does not fill on page load (account-store logins wait for a pick). The local store (Login Data) holds 0. The login page has #username and #rememberMe. The profile is not signed in to mzio. Do not type the password.
- chrome-on-demand: alcor's browser Docker restarted at 06:04 MST 2026-11-14 and the shared Chrome container did not come back. Only seat_open runs Chrome ensure(), so cdp/navigate/seat_close on an existing seat fail with 502 json/version. Seat 39eLdpHhQumcLN_g (mine, tony profile) is still recorded in seats.json, and seat_close on it failed. A seat_open to restart Chrome was interrupted by Tony and may have run: check seats.json and docker ps first. Also open: why Docker restarted.

## Workers

## Closed
- fullscreen-windows
- alcor-browser-cleanup
- seat-close-instructions
- reap-reach
- mzio-account
- productivity-browser-separation
- productivity-tab-leak

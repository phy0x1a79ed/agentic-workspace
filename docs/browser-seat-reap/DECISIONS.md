# mzio-signup — DECISIONS

## mzio-account — 2026-11-13

Question: how do you create an mzio account when mzio.io has no sign-up button?

Decided: mzio accounts live on Keycloak at auth.mzio.io, realm `mzio`. The marketing site never links to it. Open https://auth.mzio.io/realms/mzio/account/ and click "New user? Register". Tony filled in the form over VNC with txyliu@student.ubc.ca, choosing academic use, and verified the email.

Landed: the account exists and is verified. Nothing was committed.

## productivity-browser-separation — 2026-11-13

Question: should productivity's browser use be kept separate from the session seats?

User: "gotcha, nvm, good enough".

Decided: keep what is there now. Productivity has its own service window, and only it can drive that window (`productivity_only`). It shares Chrome and the tony profile with the seats on purpose, so seats can use Tony's sign-ins.

Landed: no change.

## productivity-tab-leak — 2026-11-13

Question: why are there two Teams tabs, and is productivity leaking tabs?

Decided: yes, but only for Teams. `service_tab` reconnects to an existing tab by matching its origin against `service_sites`. Teams was configured as https://teams.microsoft.com, which redirects to https://teams.cloud.microsoft, so the match missed and each reconnect opened a new Teams tab. The other sites stay on their configured origin.

Landed: on alcor, /var/lib/awm/browser/browser.json now sets `service_sites.teams` to https://teams.cloud.microsoft. The backup is browser.json.bak-20261113. This file is edited by hand and is not in the repo. Both Teams tabs were closed; productivity `chat_read teams` reopened one tab and works. The new URL takes effect at the next browser-plugin restart.

## seat-close-instructions — 2026-11-14

Question: who closes a session's browser window when the session is done?

User: "1 and 2 please". Option 1 closes a seat when its session ends. Option 2 tells the session to close it.

Decided:
- `seat_open` answers a `when_done` line, and its description carries the same sentence: call `seat_close` when done, and the browser also closes the seat when the session ends.
- The browser closes a seat when its session ends. reap-reach below sets how it learns that.

Landed: release 2e3b540d (the reminder and a first reaper), reworked in release 8435a1ab.

## reap-reach — 2026-11-14

Question: how does alcor's browser learn that a session on another node ended? The first reaper read reflection on each seat's node from alcor. alcor reaches reflection only on mizar: its altair edge is a service key that opens no tools, and it holds no edge to the other nodes.

User picked "Owner's node reports", then said "clean up the old reaper if obsolete".

Decided: each node's reflection sends its live session ids and names to the hidden browser verb `sessions_live`, on every session start and stop and once at boot. A stopped session is left out even if its provider still lists it. A list with a provider error is not sent. The browser closes the calling node's seats whose session is not named. Only a caller that is no session and did not hop may call the verb, so a node speaks for its own seats alone. The alcor-side session watch was removed.

Landed: release 8435a1ab. Verified: capella and altair report `ok` in alcor's port.log, altair's refusals stopped, the live altair seats stayed open, a session's call to the verb was refused, and a node-level call from mizar closed a test seat.

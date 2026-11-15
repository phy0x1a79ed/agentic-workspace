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

## fullscreen-windows — 2026-11-14

Question: should alcor's Chrome windows be full screen, and open that way?

User: "I meant maximized. 1920 x 1080?"

Decided: every window opens maximized with no title bar, keeping Chrome's tab strip and address bar, on a 1920×1080 display. An openbox rule in the image does it, so seats, service tabs and pop-ups all follow. Raising a seat's window maximizes it instead of restoring it to normal. Duplicate tabs came from the Teams redirect (productivity-tab-leak). The role's Teams default now carries the fix, so a role run keeps it.

Landed: cloud f371785 on dev (feat/browser-maximized 0786535), applied to alcor with a recheck of changed=0. Not in cloud release, because dev holds unverified commits from other scopes. awm release 9e69278d for the raise. Verified: a new seat window measures 1919×1079 at (0,0) on a 1920×1080 screen. The raise path is not seen live.

## chrome-saved-login — 2026-11-15

Question: alcor's Chrome saves Tony's mzio login. How does an agent use it without typing the password?

User: "Why cant they just pick the account?", then "Can we build something that would let the interaction work?", then picked "pick_login only".

Decided: the browser verb `pick_login(seat, tab, selector, choice)` clicks the field and picks the Nth entry of Chrome's saved-login list with xdotool on the container's display. Chrome draws that list outside the page, so page-level key events never reach it. Chrome fills the value, and the caller never sees it. The verb answers an error when the field stays empty. A username-first site takes one pick per page. No generic display-input verb exists, so an agent can fill only what Chrome already saved.

Landed: awm release 1d928f90 (421ed358). cloud 96b44e8 adds xdotool to the image, merged to cloud dev as 03f4b8b and applied to alcor with a recheck of changed=0. Verified: a seat signed in to mzio through two `pick_login` calls and two clicks on Sign In.

## chrome-on-demand — 2026-11-15

Question: what happens when alcor's shared Chrome stops under live seats? It stops when its Docker restarts, as alcor's daily unattended upgrade did at 06:03 on 2026-11-14, and when the browser_chrome role rebuilds the image. Its windows die with it. Only seat_open started it again, so every call on a recorded seat failed with a 502.

User picked "Restart, clear error".

Decided: every call on a seat of the shared Chrome starts Chrome when it is down. A seat whose window died answers "this seat's window is gone, as after a Chrome restart: call seat_open for a new one", and the browser forgets it. Lost windows are not reopened. The role keeps dropping a Chrome that runs an older image.

Landed: awm release feba644d. Verified: with awm-chrome removed under a live seat, `tab_list` on that seat started Chrome in under 3 seconds and answered the message above.

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

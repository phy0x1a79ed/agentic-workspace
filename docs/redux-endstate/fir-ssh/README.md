# fir-ssh: Slack seats on altair cannot ssh into fir

> A pressing event is that Slack seats on Altair cannot SSH into FIR. That's FIR.

## Finding

The fault is on fir, not in awm. The Alliance status page lists two open Fir incidents on 2026-11-06:
"Outage - Panne" (incident 1765, "Fir is currently offline due to an unknown issue") and
"Problem with login nodes" (incident 1763).

Evidence from altair and mizar:

- fir's sshd answers `Exceeded MaxStartups` or closes the connection before the banner, from altair (134.87.9.81) and from mizar alike.
- altair's awm ssh service held a live fir master until 01:54 PST. Every reconnect since 01:56 PST failed before auth. The service saw no Duo transaction, so it spent no MFA attempt and did not trip the breaker.
- The `fir` guard on altair works as designed: a bare `ssh fir` with no master is refused before any Duo push.

## Design

No awm change. The seats reach fir again through `ssh(verb="connect", host="fir")` on altair once Alliance closes the incidents.

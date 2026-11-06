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

## Tony's answer

> Please also notify the seed agent. I think that's for Beth Davenport. Maybe direct them to the status page so that they can check as well. Also, search previous journals to make sure we didn't do anything to tamper with fur.

- Beth's seat (`seat:slack/DM/Beth Davenport`, job 7e0091e4 on altair) had already found incident 1765 and was checking fir's ssh banner every 30 minutes. It received the status page links and the rule to reconnect only through `ssh(verb="connect")`. The seat does the watching, so no separate watcher is needed.
- The message went over plain ssh to altair's CLI, because the mizar→altair peer edge still returns 502.

## Did we touch fir?

No. The checks:

- awm is not installed on fir. The edge list marks it `managed: false`, `role: station`.
- altair's `~/.ssh/config` changed last on 2026-11-05. That change added two Arbutus worker hosts. The `fir` block and `~/.ssh/awm-ssh-guard` (2026-07-29) are unchanged.
- All agent traffic to fir between 05:00Z and 11:00Z on 2026-11-06 was routine work over the one shared master: `sbatch`, `sacct` polling, `ls`, `tail` and `scp` from Beth's seat and the metasmith E4 bench. None of it opened a new sshd connection.
- At 09:33Z Beth's seat saw its fir commands hang. At 09:56Z it dropped the hung master and tried to reconnect. Every reconnect since then has failed before login, and none of them used a Duo attempt. The hang came first. Dropping the master did not cause the outage.

# lost-pushes

## Tony's words

> it looks like there is a bug in how seats work. retired seats came back

## What exists today (survey, 2026-11-06)

- Steven wrote at 18:32:21Z and Kalen at 18:32:23Z. Neither line reached an agent. The last push any altair transcript received was Steven's at 2026-11-05 22:12Z, and no other inbound DM arrived in between, so the loss is these two messages, not a day-long outage.
- Kalen's holder 7c76de92 was killed at 18:31:44Z. Its push came back "gone", which releases the seat and throws the message away (`push.py:73-81`).
- Steven's holder 31bd528d was alive and idle until 18:37Z, yet got no push. The altair gateway restarted at 18:33:25Z, inside the push's 120 s retry window. The altair gateway logs no inbound peer calls, so its side shows nothing. social runs on alcor (redux R129), and its `social.log` there holds the outcome.
- At 18:41:23Z Steven's new agent got `mode "unknown" may not run a verb on a peer` on its first `social.send`. Its retry at 18:41:47Z succeeded.
- Today a push that fails is dropped silently: social logs one line, and only `social history` keeps the message.

## Decision

Tony, 2026-11-06: "this is sensible" and "we should also consider some queing".

1. A push that fails, or finds its holder gone, never drops the message.
2. social treats that seat as having no live holder. The message goes into the seat's queue, and the new-seat routine starts a fresh agent with it.
3. The build reads alcor's `social.log` to confirm why Steven's 18:32Z push to a live holder failed.

The queue and the seat states belong to the `seat-states` topic.

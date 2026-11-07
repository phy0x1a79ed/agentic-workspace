# handoff

## Split with plugin-redux (2026-11-06)

plugin-redux/interview takes DESIGN.md seat items 1-8 into the Rust social port. Its comms worker builds them: plugin-redux/comms, job e44d901a, scope awm/port-comms. keeper.py goes.

This session keeps the seat side: the `agentic-commons` project and its `seats/<slug>` scopes, the SOP and server-brief text, retiring the `opal-seat-holder` skill, and the config values (sent 2026-11-06). Discord `/new-agent` lived in the reporting service, which the redux already removed. social's Discord connector registers only `/approve`, so nothing is left to remove in code.

Brief paths given to the port: `docs/seat-sop.md` and `docs/briefs/<platform>-<server-slug>.md` in the agentic-commons checkout.

## Decisions (2026-11-06)

Tony: "approaching weekend so that is safe. I doubt rust social will land in days...", "just drop #bots, its never used", "hallam hive can be cooperative, it is invite only lab people. it is bascially agentic group chat since slack has the register bot blocker".

1. Seats stay paused over the weekend of 2026-11-07, with temporary seats on request. Tony doubts the Rust social lands within days, so whether the Python seats come back meanwhile is open again on Monday 2026-11-09.
2. Discord #bots gets no seat.
3. Discord Hallam Hive #general is a channel seat at the cooperative level, on altair. It is invite-only and lab-only, and serves as the lab's agentic group chat, because Slack blocks registering further bots.

# edge-cases

## Tony's words (redux users.md R126, 2026-11-02)

> the current system works. a main slack seat monitors for new DMs. it notifies me and spawns a seat on first DM by someone else.

> slack main handles those edges cases and uses judgement. can also ask me

R126 item 5 names the edge cases: an @Opal mention in a channel, and a sender from outside the lab.

## Difference from today's decisions

The seat-routing decision (0da96df1) replaces the long-lived slack-main session with a deterministic routine. R126 gives slack-main two jobs that a routine cannot do by judgement: notify Tony on a first DM, and handle the edge cases.

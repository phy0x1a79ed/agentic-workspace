# edge-cases

## Tony's words (redux users.md R126, 2026-11-02)

> the current system works. a main slack seat monitors for new DMs. it notifies me and spawns a seat on first DM by someone else.

> slack main handles those edges cases and uses judgement. can also ask me

R126 item 5 names the edge cases: an @Opal mention in a channel, and a sender from outside the lab.

## Difference from today's decisions

The seat-routing decision (0da96df1) replaces the long-lived slack-main session with a deterministic routine. R126 gives slack-main two jobs that a routine cannot do by judgement: notify Tony on a first DM, and handle the edge cases.

## Decisions (2026-11-06)

Tony: "this removes slack-main from before into a determinisitic mechanism".

1. slack-main is gone. The new-seat routine and social do its jobs deterministically.
2. "first DM action is sensible": a first DM from a new person notifies Tony (request_human) and starts their agent.
3. "sender cant send from outside lab pretty sure, but yes.": a sender outside the lab gets no agent. The routine holds the message and asks Tony.

## Open: what one seat covers

Tony: "lets thing through 1 dm channel per seat. I dont want to arbitrarily constrain. this may be useful... idk"

- Per chat (one DM or one channel per seat): simple, but a person who writes in a DM and also @mentions Opal in a channel gets two agents with split context.
- Per person (all of one person's DMs, group DMs and @Opal mentions go to their one agent, which answers in the chat the message came from): it matches the per-person home scope `seats/<person>`, the `user:<person>` tag, and R126 item 3 ("A person's replies go to their own seat from the first message").

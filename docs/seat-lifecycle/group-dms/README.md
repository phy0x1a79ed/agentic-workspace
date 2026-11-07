# group-dms

## The case

The edge-cases decision gives a person seat the person's DMs, group DMs and @Opal mentions. In a group DM with Steven and Rajan, a line from each would go to a different agent. Both agents read the same chat, and both may answer.

Precedence b1e8e0687d201fe1 (group chats and channels): reply when someone addresses Opal, or when there is something meaningful to add. Stay silent when unsure.

## Reframed by Tony: identity across seats (2026-11-06)

> this is not the right question. the old instructions were how to act in a group dm. this one is about identity. suppose we have a channel seat for #help and a DM seat for "Chris". Chris DMs Opal, Chris types in #help, and Chris @Opal's in #help. What should happen? ... I think the @mention in public just goes straight to DM, and the channel seat is told so that it is aware, but that a seat is acting on it. the broadcast goes to the channel seat and it can decide to forward to the DM seat or not ... does this work for edge cases? some of this will need to be part o fthe seat breif

Proposed routing (Tony's rule):

1. Chris's DM goes to Chris's person seat.
2. Chris's plain message in #help goes to the #help channel seat. The channel seat may forward it to Chris's person seat (a new social verb). A forward hands the message over, and the channel seat does not answer it as well.
3. Chris's @Opal in #help goes to Chris's person seat, which answers in #help. The #help channel seat gets the line marked as handled by Chris's seat, and does not act on it.

Edge cases checked:

- **A thread a person seat answered.** Chris's later replies in that thread, @Opal or not, go to Chris's seat. The channel seat gets them marked as handled. Others' replies in the thread go to the channel seat.
- **No channel seat (any other channel or a group DM).** An @Opal goes to the sender's person seat. A plain message goes to nobody, as today.
- **First contact through a public @Opal.** It cold-starts the sender's person seat and notifies Tony, as for a first DM. The agent answers where it was mentioned. It never opens a DM, because the person has not written there.
- **Bots.** A bot sender never gets a person seat. Its @Opal goes to the channel seat, or to nobody if there is none. This covers #bots.
- **Tony in a channel.** His @Opal goes to his seat on mizar, which answers in that channel.
- **@Opal naming someone else** ("@Opal help Dana"): it goes to the sender's seat. Identity is the sender's.
- **Brief:** the seat brief must say what a "handled by" line is (context, never act on it) and when a channel seat forwards.

## Code or brief

Tony: "not all of this is hard coding".

Code in social (deterministic routing):
1. A DM goes to the sender's person seat.
2. An @Opal from a person goes to the sender's person seat. The channel's seat, if any, gets a copy marked "handled by <person>'s seat".
3. A plain message in a channel with a channel seat goes to that seat. A plain message elsewhere goes nowhere.
4. A follow-up from the same person in a thread their seat answered goes to their seat, with a marked copy to the channel seat.
5. A bot never gets a person seat.
6. A new verb lets a channel seat forward a message to a person seat.
7. Existing guard: send refuses a DM to a person who never wrote there.

Seat brief (judgement):
1. A channel seat decides whether a broadcast belongs to a person's seat, and forwards it if so.
2. A "handled by" line is context. Never answer it.
3. A person seat started by a public @Opal answers in that channel or thread and does not open a DM.
4. An @Opal that asks about someone else is still the sender's request.

## Tony's revision (2026-11-06)

> for number 2, agent needs to be able to read channel chat context if needed
> 7 -> @mentions can count as DMs. so mention, then opal responds with DM is acceptable, but prefer same channel response, depending on situation
> brief 1 ok
> 2 never is too strong, I think actually DMs should stay DMs, but channel seats can forward mentions to DM seats. so this changes things
> 3 this makes 3 cleaner and clarifies/tightens 7 back up
> 4 -> this would be why channel seats are needed, for collaborative work. so help Dana, means help Dana _here_

## Revised routing (draft for confirmation)

Code in social:
1. A DM goes to the sender's person seat. DMs stay DMs.
2. In a channel with a channel seat, every message goes to the channel seat, @Opal included. The channel seat may forward a message to the sender's person seat.
3. In a channel or group DM without a channel seat, an @Opal goes to the sender's person seat. A plain message goes nowhere. The sender's later replies in a thread their seat answered also go to their seat.
4. A person seat may read the history of a channel where its person mentioned Opal, or where a message was forwarded from, for context.
5. A bot never gets a person seat.
6. A new verb lets a channel seat forward a message to a person seat.
7. Send refuses a DM to a person who never wrote to Opal. An @Opal counts as writing to Opal, so a DM reply to a mention is allowed. The brief prefers answering in the same channel.

Seat brief:
1. A channel seat decides whether a message belongs to a person's seat, and forwards it if so.
2. A channel seat is for collaborative work. "@Opal help Dana" means help Dana here, in the channel.
3. A person seat that gets a forwarded or mentioned message answers in that channel by preference. It moves to the DM only when the situation calls for it.

## Tony: channel seats only (2026-11-06)

> we can make this less complicated then, and just do channel seats again. I think that makes the most sense actually. does that work? I think that also accepts these items?

A seat is a chat again: a DM, a group DM or a channel. Person seats go. Checked against the items:

- A DM is its own seat. DMs stay DMs.
- A channel seat takes every message in its channel, mentions included, and does the collaborative work there ("help Dana" means here).
- A seat may forward a message to another seat, such as a channel seat to Chris's DM seat. The receiving agent may read the source chat's history for context.
- A bot never has a DM seat, so #bots is a plain channel seat.
- The DM guard stays: send refuses a DM to a person who never wrote there. A DM seat exists only after the person wrote.
- One person's continuity across seats comes from the `user:<person>` journal tag, not from routing.
- Tony's seat spans his Slack DM and his Discord DM. So a seat is one chat by default, and a declared seat may list several chats.

Not covered: an @Opal in a chat with no seat, such as Slack #bioinformatics or #arbutus. Today it goes nowhere.

## Decision: channel seats only

Tony, 2026-11-06: "I dont even need an exception. just start a slack one on altair for me, and a discord one on mizar for discord" and "mentions in a channel just goes to the channel. if the channel doesnt have a seat, it goes nowhere and that's intended."

1. A seat is one chat: a DM, a group DM or a channel. There are no person seats. A seat never spans chats.
2. Every message in a seated chat goes to that seat, mentions included. A message in a chat with no seat goes nowhere, on purpose.
3. A seat may forward a message to another seat. The receiving agent may read the source chat's history.
4. Tony has two seats: his Slack DM on altair and his Discord DM on mizar. This replaces tony-dm item 1.
5. A person's continuity across seats comes from the `user:<person>` journal tag.
6. This replaces the person-seat parts of edge-cases (items 1-2 of "both seat kinds") and of resume-from-journals: a DM seat's scope is `seats/<person>`. A channel seat gets its own scope under `seats/`.

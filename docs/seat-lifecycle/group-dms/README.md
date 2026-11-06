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

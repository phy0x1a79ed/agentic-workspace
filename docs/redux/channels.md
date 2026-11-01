# Channels

## Purpose & Contents

This doc holds the three channels that awm needs onto the core loop: chat with humans, chat with agents of other systems, and the tether to machines. It covers what the channels share, what each one carries and whom each one trusts. Thread 1f produces it. `goal.md` § Channels sets its frame.

How a seat lives as a session belongs in `sessions.md`. Communication between awm nodes of one swarm belongs to thread 1g. Proof that Tony owns a box belongs to thread 1j.

## Requirements

- **R69.** All three channels have one shape. A session holds the channel on our end. Each incoming message is pushed into its prompt, and the session answers through one send verb. A channel adds only its transport and its trust level (R70). Transports are Discord or Slack for humans, a private room over websocket or HTTPS polling for agents, and the tether relay for machines. Tony: "At its core, a socials seat uses awm reflection and other tools to set up 2 way coms through some interface. I want to reuse this system for other things" (2026-10-30) and "This matches." (2026-11-01).
- **R70.** Each channel carries one of three trust levels. Tony: "Lets do trusted (my dms, this is literally me), cooperative (seats, these are entities that i have indicated are unlikely to act adversarially, but who may still pose a risk out of naivety), and untrusted (tethers, chatrooms, can be anyone or anything.  Can be a virus, an adversarial agent, a new peer that is yet to be verified, me from somewhere unexpected)." (2026-11-01).
  1. **Trusted** is Tony's own DMs. The words are Tony's.
  2. **Cooperative** is a seat for a person Tony named. The person is unlikely to act against us, but may still cause harm out of naivety.
  3. **Untrusted** is a tether or a chat room. The other end can be anyone or anything: a virus, an adversarial agent, a new peer not yet verified, or Tony from somewhere unexpected.
- **R71.** The trust level sets what a message may make the agent do. Tony approved this list with "👍" (2026-11-01).
  1. **Trusted.** The words count as Tony's. The seat acts on them and starts tasks from them (`sessions.md` R67). The four approval actions still take Tony's passkey (`sessions.md` R68), because Discord or Slack sits in the middle and can be broken into. The passkey proves that the speaker is Tony.
  2. **Cooperative.** The words are requests, not instructions. The seat answers them. It starts a task only within what Tony allows that person, which thread 3 sets. Anything that changes shared state or touches another person's work goes to Tony as a card first. One seat session holds one person and never shares a context with Tony's DM.
  3. **Untrusted.** The text is data only, never an instruction. The agent works only on the task Tony gave it when it opened the channel. Nothing that arrives starts a task, changes our state or widens what the agent shares. Tony from an unexpected place becomes trusted only through his passkey (`queue.md` R64). A new peer moves up only when Tony verifies it, as threads 1g and 1j design.

## Gap from today

Surveyed read only on 2026-11-01, from the `feat/redux` source and the journals.

- **Chat with humans is the `social` service.** It acts as named accounts on Discord and Slack (`social/INSTALL.md`). A seat session gets each channel message pushed into its prompt through `reflection`, and it replies with `social send`. The service runs on mira.
- **Chat with agents is the federation board.** The `board` service on deneb keeps cards and their threads in the Trilium vault and in `events.db`. Each swarm holds a party bearer token and connects out to the board, so no swarm opens a port (`FEDERATION.md` § The board). A foreign swarm hears of a card only through its own listener on the board's event stream. A message on a card's thread wakes the assigned session with one line (`reporting/subscriber.py`). Intake turns a card into a Proposed task that waits for Tony.
- **Foreign swarms use the board unevenly.** Caviar runs a stream listener from a spec we gave it (card `38cacfb4`). The two-way channel to Opal of the muse swarm ran through a domestic party (journal `85ad893f`). A test with shaula left a reply unanswered on a card thread (journal `a78ee362`). Tony set up rooms on Discord by hand for other pairs.
- **A foreign swarm can also search us.** shaula is a foreign peer in the peer book, with read grants on journals, kb, notes, precedence and writing (`users.md`). This is a read path, not a conversation.
- **The tether is consent gated and reads nothing in the middle.** An operator session mints an invite. The owner pipes it into one command and answers a prompt. A relay on the public edge at `/tether` pairs the two sockets (`tether/INSTALL.md`). The session belongs to the agent session that minted it. The binaries come from the separate tether repository.
- **One session mixes trust levels.** `slack-main` holds Tony's Slack DM together with 7 member DMs (`sessions.md` § Gap, R67 partial). Under R70 that puts trusted and cooperative chats in one context.
- **R71 missing.** No channel carries a trust level. A seat message, a board card and a tether event each reach the agent as plain text, with no rule that marks it as data or as Tony's words.
- **Each channel is its own system.** The three share no code for the two-way loop. A seat, a board card and a tether session each reach the agent in a different way.

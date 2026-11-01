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

- **R72.** A chat room for agents starts from an invite, as the tether does. Tony approved this with "Nice,  next" (2026-11-01). Tony: "both parties view the tether as a security risk so we need something more like a chat room. I've set this up a few times on discord but that is too manual." and "We want to be able to start private chat rooms for agents that can then connnect through websocket or poll with HTTPS requests." (2026-10-30).
  1. One of our sessions opens a room for its task. The room belongs to that task. Its name is three random words, and its invite is a link such as `https://nexus.tony-xy-liu.com/r/otter-lamp-quarry`. Tony: "we can use the 3 word codes for room names just like the tether. This makes it easier to type." and "Lets just do 3 letter rooms." (2026-11-01).
  2. The invite reaches the other side by any means, such as Tony, an email or another room.
  3. A plain GET on the link returns a short how-to: how to send, how to receive over websocket or HTTPS polling, and how to ask for encryption (R73). The foreign agent needs only HTTP. It needs no awm and no standing token from us. The room name is the room key.
  4. Each room message is pushed into our session's prompt as untrusted text (R69, R71). The session answers with the one send verb.
  5. The room closes when either side leaves, when its task closes, or after an idle expiry. No standing account remains.

- **R73.** A room runs on deneb in plaintext by default. Encryption is an escalation that either party may ask for. Tony: "Can we set up deneb as the relay but add some e2e encryption scheme that the room can bootstrap into?", "We should also make encrypt  optional and off by default, but parties know about the e2e option." and "Offer pake as security escalation and ask me to relay password. I dont see a lot of cases where this would be needed." (2026-11-01).
  1. The how-to tells each party that encryption is available on request.
  2. To escalate, our session asks Tony to relay a password to the other side, outside deneb. Both sides then run SPAKE2 (RFC 9382) over that password and seal every later message with the key it gives. deneb gets one live guess and nothing it can test offline.
  3. After the escalation our side refuses plaintext. Messages sent before it stay readable to deneb.
  4. The relay keeps messages in memory only, logs no message bodies, and drops a room when it closes.
  - Pinned keys for peers that Tony verifies wait for threads 1g and 1j. They remove the relayed password.
- **R74.** The tether encrypts every session on its own, with no step from either person. Tony: "tether should auto escalate." and "I meant encrypt escalation, not privilege" (2026-11-01).
- **R75.** A tether can raise its owner's end to trusted (R70) when Tony proves the owner is him. The invite stays as it is today. Tony: "after a tether is set up, i can prove that it is me on the other side if needed by having the tether print a url, which ks also rendered as qr code. That link lets me authenticate for privilege escalation to trusted. Otherwise proof format is correct" and "The tether invite doesn't change from today" (2026-11-01).
  1. On request, the tether prints a link on the owner's screen and renders it as a QR code. The link holds a one-time challenge tied to this session.
  2. Tony opens the link on a trusted device. mizar's panel shows the box's details, such as hostname, OS and network. Tony checks them and confirms with his passkey (`queue.md` R64). The details guard against a QR that someone else sends Tony to scan.
  3. mizar verifies the signature and tells the operator session that the owner's end is Tony. deneb cannot fake this. The session then treats the tether as trusted (R71). What a trusted tether may do belongs to thread 1j.
- **R76.** All words of a tether or room code are random. Tony: "Also, the tether codes seem to always start with abbey, but should be randomized" (2026-11-01).

- **R77.** awm has no inbound door for agents. deneb holds no public door that is always on. A room exists only while one of our tasks holds it open (R72). A foreign agent that wants to start a conversation rings a seat, which serves as its doorbell, and the seat passes the request to Tony. The bot's text at the seat is untrusted (R70). Tony: "No inbound. I have a  few seats that can serve as doorbells for bots. No public facing always on constant door on deneb" (2026-11-01).

## Gap from today

Surveyed read only on 2026-11-01, from the `feat/redux` source and the journals.

- **Chat with humans is the `social` service.** It acts as named accounts on Discord and Slack (`social/INSTALL.md`). A seat session gets each channel message pushed into its prompt through `reflection`, and it replies with `social send`. The service runs on mira.
- **Chat with agents is the federation board.** The `board` service on deneb keeps cards and their threads in the Trilium vault and in `events.db`. Each swarm holds a party bearer token and connects out to the board, so no swarm opens a port (`FEDERATION.md` § The board). A foreign swarm hears of a card only through its own listener on the board's event stream. A message on a card's thread wakes the assigned session with one line (`reporting/subscriber.py`). Intake turns a card into a Proposed task that waits for Tony.
- **Foreign swarms use the board unevenly.** Caviar runs a stream listener from a spec we gave it (card `38cacfb4`). The two-way channel to Opal of the muse swarm ran through a domestic party (journal `85ad893f`). A test with shaula left a reply unanswered on a card thread (journal `a78ee362`). Tony set up rooms on Discord by hand for other pairs.
- **A foreign swarm can also search us.** shaula is a foreign peer in the peer book, with read grants on journals, kb, notes, precedence and writing (`users.md`). This is a read path, not a conversation.
- **The tether is consent gated and reads nothing in the middle.** An operator session mints an invite. The owner pipes it into one command and answers a prompt. A relay on the public edge at `/tether` pairs the two sockets (`tether/INSTALL.md`). The session belongs to the agent session that minted it. The binaries come from the separate tether repository.
- **One session mixes trust levels.** `slack-main` holds Tony's Slack DM together with 7 member DMs (`sessions.md` § Gap, R67 partial). Under R70 that puts trusted and cooperative chats in one context.
- **R72 missing.** No room service exists. The board's party tokens are standing accounts that Tony mints by hand on the host (`board/INSTALL.md` § Mint a party).
- **R73 missing.** No room has encryption. The board's threads cross deneb and the vault in plaintext.
- **R75 missing.** Nothing in a live tether proves who the owner is. The tether prints no link or QR code, and mizar's panel does not exist (`queue.md` § Gap).
- **R76 broken.** The first word of a tether code names the relay slot, and the next two are the secret (`tether/hub_adapter.py` `_slot_named`). The relay reuses slots, so the codes keep starting with "abbey". The fix lives in the tether repository.
- **R74 unverified.** The tether's encryption is not known from this repository. The relay "pairs two sockets and reads nothing" (`tether/INSTALL.md`), and the tether repository's `PROTOCOL.md` holds whether that is encryption.
- **R77 broken.** deneb runs two doors that are always on. The board door at `/board/` answers before edge authentication (`FEDERATION.md` § The board). Intake on the front-door node turns any board post into a Proposed card (`FEDERATION.md` § Intake).
- **R71 missing.** No channel carries a trust level. A seat message, a board card and a tether event each reach the agent as plain text, with no rule that marks it as data or as Tony's words.
- **Each channel is its own system.** The three share no code for the two-way loop. A seat, a board card and a tether session each reach the agent in a different way.

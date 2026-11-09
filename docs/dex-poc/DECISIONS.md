# DEX POC: Decisions

One section per closed topic, in closing order. Open state is in `STACK.md`.

## freshness (2026-11-09)

**Question.** How does a node learn that what it holds is out of date, given that time-based staleness is not wanted?

**User's words.** "we dont want time based staleness." "transport keepalive is pretty low level and established protocol"

**Decided design.** A name is either subscribed (the node holds what the origin last announced) or disconnected (unknown). There is no `max-age` and no `stale-while-revalidate`. Three events drive updates:

- Publish: the origin announces name, hash and sequence to subscribers, who fetch the bytes by hash and swap. The publish event is the activation unless a node's policy holds it back.
- Subscribe or reconnect: the node sends the hash it holds, the origin answers `304` or the new hash (HTTP revalidation, fired by connecting).
- Use: messages and replies carry the hashes they depend on; an unseen hash tells the receiver to fetch first. This replaces the bounded wait in Q6.

Routing to a singleton needs no freshness check: a "moved" reply or a connection failure triggers re-resolution. Behaviour while disconnected is policy per name: CLAUDE.md keeps its last version, an effectful call fails at the seam. A transport keepalive is the only clock, stays inside the connection layer, and its only output is the disconnected event.

Each mutable name has one origin. Whether that sits within C3 is still to confirm (see `claude-md-sync`).

**Landed.** Input to the `handoff` brief. Revises design section 7 and Q6.

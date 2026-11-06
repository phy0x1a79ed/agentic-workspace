# seat-brief

## Tony's words

> need to consolidate previous decsions into brief for seats on altair. need to also have a more general version for other seats like discord

## What exists today (survey, 2026-11-06)

- `docs/redux/drafts/agentic-commons/docs/seat-sop.md` (draft, not live) covers trust levels, the main Slack seat, a person's seat, first contact, voice, group chats and reaching Tony. It is Slack-lab specific throughout.
- `~/.claude/skills/opal-seat-holder/SKILL.md` (live, paused) is slack-main's SOP, with the per-person agent brief inside it.
- Kept-seat briefs live in `social.toml` `[seat.*].brief` on the social node.
- Each restart wrote its own brief by hand (slack-main's 18:39Z brief said "Answer anything unanswered").

Out of date against this discussion: the main Slack seat section, the trust level on each pushed line, and the brief's missing lifecycle (done mark, 4-hour reclaim, handoff journal, journal tags and frequency, the narrowed speak rule).

## Decision

Tony, 2026-11-06: "accept reccomendations".

1. A general seat SOP for every platform covers lifecycle, journaling, the speak rule, trust levels, first contact and reaching Tony.
2. One brief per server covers what is local to it. The Hallam lab Slack on altair gets clusters, tools, the tether and lab voice. Each Discord server gets its own brief.
3. The new-seat routine composes the SOP, the server brief and the seat facts: person or channel, trust level, scope, queued messages, and a previous-agent note when one is still retiring.
4. The SOP and the server briefs live in `agentic-commons/docs`. The `opal-seat-holder` skill retires.

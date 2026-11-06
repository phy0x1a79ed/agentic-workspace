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

## Decision: both seat kinds

Tony, 2026-11-06: "lets make both possible. since there are places like the bot channel and hallam-hive general on discord. both are needed."

1. A **person seat** takes every message from one person: DMs, group DMs and @Opal mentions. Its agent answers where the message came from.
2. A **channel seat** takes every message in one channel, such as the Discord bot channel or Hallam Hive #general.

## Seat names today (survey, 2026-11-06)

Tony: "can we review how the keyed prefix looks today? I think it can be improved."

- social names a holder `seat:<app>/<server>/<channel>`, or `seat:<app>/DM/<person>` for a DM (`awm/social/seats.py:28-31`). `<person>` is the platform display name, with spaces and capitals.
- Names in use: `seat:slack/DM/Steven Chen`, `seat:slack/slack-main`, `seat:slack/arbutus`, `seat:discord/unimatrix0/notifications`.
- A Slack DM name carries no workspace, while a channel name does.
- The Remote Control name swaps each `/` for `-`: `seat-slack-DM-Steven-Chen`.
- The person's slug differs between places: `Steven Chen` in the name, `steven-chen` in the scope and the tag.
- A kept seat in social.toml has a third name, its `[seat.<name>]` key.

## The pushed line today (survey, 2026-11-06)

Tony: "Can you pull some examples of the prefix from actual messages?"

Real lines from seat transcripts:

- `slack from Steven Chen [channel=D0CAGBM8ACQ message_id=1793340591.584919]: is it running?`
- `slack from Steven Chen ↳1793825822.808289 [channel=D0CAGBM8ACQ message_id=1793916761.608339]: can you update my analyses ...`
- `slack from Beth Davenport [channel=D0C9RM3JTDJ message_id=1793814379.554989]: [+1 file]`
- `slack from Tony [channel=C0C9XNR77ML message_id=1792715091.033399]: approve` (a channel, #arbutus)
- `discord from john-shaula-agent#1257 [bot] [channel=1560400515630760049 message_id=1564718010223038596]: Opal, I don't hold ...`
- `discord from phyberosis [channel=1560400515630760049 message_id=1564721115358564405]: no worries, we can run tests then.`

The deployed code (`awm/social/seats.py:79-96`) adds the seat's trust level, `slack [cooperative] from ...`. No transcript shows it yet.

What the line lacks:
- The chat's name and kind. `D0C...` is a DM and `C0C...` a channel only by Slack's id convention. A Discord channel is a bare number.
- The workspace or server.
- A stable person key. The sender is a display name (`Steven Chen`, `phyberosis`), not the `<person-slug>` of the scope and tag.

## The pushed line: Tony's review (2026-11-06)

> here's a prefix i see: "slack from Steven Chen ↳1793825822.808289 [channel=D0CAGBM8ACQ message_id=1793916761.608339]: ' useless: "slack from" "↳1793825822.808289" "channel=D0CAGBM8ACQ" -> should be just DM "message_id=1793916761.608339"

What the ids do today: the agent copies `channel` and `thread` into `social send` to answer in the same chat and thread, and `channel` with `message_id` into `social react` (`awm/social/hub_adapter.py:237-294`). Dropping them from the line means social must fill them in.

## Decision: the pushed line

Tony, 2026-11-06: "thread needs id for lookup", "access level is said once in the beginning?" and "if we do channel or user, we report the other per message. seat steven -> DM, etc / seat arbutus [Ryan] etc".

1. The line carries only what the seat does not already fix. A person seat names the chat. A channel seat names the sender.
   - Person seat (Steven): `[DM] can you update my analyses ...`, `[#arbutus] ...`
   - Channel seat (#arbutus): `[Ryan] approve`
2. A thread reply keeps its thread id for lookup: `[DM ↳1793825822.808289] just using a strict ...`, `[Ryan ↳1792715091.033399] ...`.
3. The trust level is stated once, in the agent's starting brief, not on each line.
4. No platform word, no `from`, no channel id and no message id. `send` and `react` from a seat's agent default to the chat, thread and message of its latest line. The agent names another chat by its label (`DM`, `#arbutus`) or a thread by its id. `social history` still returns all ids.
5. Seat names follow the same keys: `seat:<app>/<server>/@<person-slug>` and `seat:<app>/<server>/#<channel-slug>`. `<person-slug>` matches scope `seats/<person>` and tag `user:<person>`.

# tony-dm

## What exists today (survey, 2026-11-06)

- Tony's Slack DM with Opal is D0C9GMGRSFP. It is in the slack-bot account's `trusted_chats`, so its lines are trusted: Tony's words are instructions (`channels.md` R70).
- slack-main held it, and Tony directed the seats from it.
- Redux `sessions.md` R67 and R68: Tony's DM seat chats, answers quick questions, and helps him work the queue. It acts on the queue only through his passkey.
- Redux `users.md` R126 item 2: a lab seat never reads Tony's DM. `chat_bound` enforces that in social.

## Decision

Tony, 2026-11-06, to "Tony's DM is a person seat like the others": "yes -> mizar, responds to slack and discord".

1. Tony's seat is one person seat that spans his Slack DM with Opal (D0C9GMGRSFP) and his Discord DM with the bot. Its agent answers in whichever DM the message came from.
2. It runs on mizar, at the trusted level, with R67 and R68 in its brief.

## Notifications in the Discord DM (survey, 2026-11-06)

Tony: "discord DM also has notifications?"

Yes. `config.operator_dm()` (`awm/service_components/config/awm/config/operator.py:14`) defaults to discord-bot, channel `dm:188743359983124480`, Tony's Discord DM. Three senders post there through the bot:

- the social keeper's seat-down and recovery notices (`social/hub_adapter.py:1643`)
- cch keep's daemon notices (`cch/keep_adapter.py:140`)
- ssh's lock alerts and notify self-test (`ssh/service.py:1733`)

These are the bot's own messages. They are not inbound, so they never start the seat agent.

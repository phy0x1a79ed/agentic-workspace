# seat-start

## Tony's words

> how to seat starts work? do they go through the reflection, cch, etc and use the warm seats? so they start fast?

## What exists today (survey, 2026-11-06)

- A seat agent starts through `reflection start_other`. reflection forwards to cch `start` (`services/cch/awm/cch/lifecycle.py:226`), and cch asks the Claude daemon for a background job.
- The daemon hands over a pre-started warm spare instead of booting a new process. Its log shows `bg claimed-spare <job>` for every seat started today (e831ba99, 72d5a63f, 9e22f611, a1406294).
- Patrik's agent (a1406294): spare claimed 21:27:37.151Z, brief delivered 21:27:38.058Z, first tool call 21:27:44.108Z. That is about 7 s from start to work.
- The new-seat routine calls the same `start_other`, so it uses the same warm spares. Its extra cost is the event hop from social and one script run, a few seconds.

## Where the 7 seconds go (Patrik's agent a1406294)

| Step | Time (Z) | Gap |
|---|---|---|
| spare claimed | 21:27:37.151 | |
| brief delivered | 21:27:38.058 | 0.9 s |
| first model request sent | 21:27:40.734 | 2.7 s (session start: MCP tools, hooks, attachments) |
| first model output | 21:27:43.439 | 2.7 s (model latency on a cold 23k-token prompt) |
| first tool call | 21:27:44.108 | 0.7 s |

The model's own latency sets a floor of a few seconds. Getting the agent's first action near 1 s is not realistic. An immediate reply from social is.

Tony: "why 7 seconds? can we get that closer to 1? not important, but worth the try. -> or even a system message like "performing first time set up""

## Reaction and typing indicator (survey, 2026-11-06)

Tony: "can we just respond with eyes emoji as a reaction? can we also do teh typing indicator?"

- Reaction: the Slack app already has `reactions:write`, and both connectors implement `react`. social can add 👀 the moment a message arrives.
- Typing on Discord: a bot can show "Opal is typing…" through the channel's typing trigger. It lasts about 10 s, so social refreshes it until the agent's reply. The Discord connector does not call it yet.
- Typing on Slack: the Web API gives a bot no typing indicator. The only equivalent is the AI-app status (`assistant.threads.setStatus`, "is thinking…"). It needs the app's Agents & AI Apps feature and the `assistant:write` scope. That changes the Slack app manifest, and a workspace admin must reinstall the app.

## Decision

Tony, 2026-11-06: "react eyes only".

1. social reacts 👀 to a message that starts an agent (a cold start: the seat is empty or retiring), the moment it arrives. A message to a live agent gets no automatic reaction. Tony: "this is only for cold starts".
2. No typing indicator on any platform, and no "getting set up" reply.
3. The seat start itself stays as it is: `start_other` through cch on a warm spare, about 7 s to the first action.

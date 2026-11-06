# rc-toggle

## Tony's words

> about the seats, it would be great if on the new fleet status dashboard, I can turn remote control on and off for seats

## Split with trilium-design/interview (2026-11-06)

trilium-design/interview on mizar owns the dashboard surface: where the toggle sits and how it routes through alcor. This discussion owns the seat side. It asked three questions, queued below.

## What exists today (survey, 2026-11-06)

- `reflection remote_control` turns Remote Control on or off for a session on its node. On types `/rc <name>` and returns the claude.ai link. Off types `/rc` and picks "Disconnect this session". It refuses a session that is mid-turn.
- `reflection start_other` turns Remote Control on by default (sessions.md R146).
- A seat's holder shows in `social seats` (account, channel, holder job, node, session name). Seat sessions are named `seat:slack/DM/<person>`. Nothing in the cx roster marks a session as a seat.

## Handed off

Tony, 2026-11-06: "Can we pass this on to Trillium Design? I will just speak with that agent directly."

The seat side's two open points (how the dashboard recognizes a seat, and whether the toggle needs the passkey) went to trilium-design/interview with the recommendations: read social's seat list as the source, and no passkey. Tony settles them there. The third question ("can we sort natively in claude?") was trilium-design's own and stays with it.

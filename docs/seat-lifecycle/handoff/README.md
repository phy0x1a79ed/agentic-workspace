# handoff

## Split with plugin-redux (2026-11-06)

plugin-redux/interview takes DESIGN.md seat items 1-8 into the Rust social port. Its comms worker builds them: plugin-redux/comms, job e44d901a, scope awm/port-comms. keeper.py goes.

This session keeps the seat side: the `agentic-commons` project and its `seats/<slug>` scopes, the SOP and server-brief text, retiring the `opal-seat-holder` skill, and the config values (sent 2026-11-06). Discord `/new-agent` lived in the reporting service, which the redux already removed. social's Discord connector registers only `/approve`, so nothing is left to remove in code.

Brief paths given to the port: `docs/seat-sop.md` and `docs/briefs/<platform>-<server-slug>.md` in the agentic-commons checkout.

## Decisions (2026-11-06)

Tony: "approaching weekend so that is safe. I doubt rust social will land in days...", "just drop #bots, its never used", "hallam hive can be cooperative, it is invite only lab people. it is bascially agentic group chat since slack has the register bot blocker".

1. Seats stay paused over the weekend of 2026-11-07, with temporary seats on request. Tony doubts the Rust social lands within days, so whether the Python seats come back meanwhile is open again on Monday 2026-11-09.
2. Discord #bots gets no seat.
3. Discord Hallam Hive #general is a channel seat at the cooperative level, on altair. It is invite-only and lab-only, and serves as the lab's agentic group chat, because Slack blocks registering further bots.

## Seat side done (2026-11-07)

- `agentic-commons` exists on altair: GitHub `phy0x1a79ed/agentic-commons` (private), main checkout `projects/agentic-commons/main`. It holds `docs/seat-sop.md`, `docs/briefs/{slack-hallam-lab,discord-hallam-hive,discord-dm}.md` and `scripts/new-seat.sh`.
- 15 seats are seeded on altair: the 13 lab DM people, `tony` and `hallam-hive-general`. Each has a local git repo with DVC at `main/seats/<slug>`, a scope `seats/<slug>` registered with `scope ensure`, and a seed journal tagged `journal`, `seat`, `user:<slug>`.
- Tags `seat` and `user:<slug>` (14 people) are in the vocabulary.
- The temporary seats are retired. Kalen's handoff is 7e3766bd (metasmith aspire/dev), Patrik's is 353bb3fe (metasmith lab/patrik-star, with his unanswered request).
- plugin-redux confirmed the start shape: `start_other` gains `cwd` and `claim`, and social runs `new-seat.sh` before a new seat's first start.

## Cutover, when the Rust social goes live

1. On mizar, create the project: `awm scope create --project agentic-commons --new_project true --clone_url git@github.com:phy0x1a79ed/agentic-commons.git`. Then run `scripts/new-seat.sh tony` in its main checkout. altair cannot reach mizar, so a mizar session does this.
2. On altair, delete the PAUSED banner, then the `opal-seat-holder` skill (`~/.claude/skills/opal-seat-holder`) and its draft under `docs/redux/drafts/claude-home/`.
3. Remove `docs/redux/drafts/agentic-commons/` from the awm repo. The live copy is the agentic-commons repo.
4. Unmount the dead holders still on slack-bot seats, such as Patrik's (job a1406294, gone).
5. Keep `~/.local/share/awm/seat-pause-backup` until the new seats run a week clean, then delete it.

# keeper

## What exists today (survey, 2026-11-06)

- The keeper (`awm/services/social/awm/social/keeper.py`) reads `[seat.*]` tables from social.toml. It starts each seat's agent, resumes or replaces it when it dies, remounts its chats every 60 s, and posts down and recovery notices to the operator DM.
- `social keepers` returns no kept seats on 2026-11-06. The kept seats from October (Tony's Discord DM, unimatrix0 #bots, Hallam general, muse) no longer have tables. #bots is held by job 30bbc7a3 on deneb, which mounted it by hand.
- Redux `channels.md` R72 item 5 keeps one long-lived holder on purpose: a persistent room, such as the muse two-way link or Caviar's stream listener.
- The new lifecycle keeps no agent alive while it is idle. That is the opposite of what the keeper does.

## Decision

Tony, 2026-11-06: "the keeper design is functionally still the same, just that past 4hrs, the agent starts cold".

1. The keeper stays as the mechanism that keeps every seat: person seats, channel seats and Tony's seat.
2. The keeper changes in one way: an agent done for 4 hours retires with a handoff journal, and the seat's next message starts a new agent cold. The rest of the seat-states design applies.
3. The keeper already starts agents through `reflection start_other` on the seat's node (`keeper.py:425`). It is deterministic code in social, not a Claude session.

## What the keeper is (for Tony, 2026-11-06)

Tony: "what is the keeper? do we even need it under the new seats mechanism? can we just have seats take over?"

social has two parts today:

- **Seats** (`seats.py`, 125 lines, plus the mount, unmount and push handlers). Any session mounts a chat and gets its messages pushed. Seats start nothing and keep nothing.
- **The keeper** (`keeper.py`, 1091 lines). It covers only chats declared as `[seat.*]` in social.toml. Every 60 s it checks that each declared seat's agent runs. It starts, resumes or replaces the agent through `reflection start_other`, remounts the seat's chats, and stops retrying after repeated failures (a breaker). It asks a witness node whether a host is really down before it posts a down notice to the operator DM.

The new design already gives seats states, a queue and the cold start. The keeper's start, breaker and down-notice code is what that needs. Two components would duplicate it.

# keeper

## What exists today (survey, 2026-11-06)

- The keeper (`awm/services/social/awm/social/keeper.py`) reads `[seat.*]` tables from social.toml. It starts each seat's agent, resumes or replaces it when it dies, remounts its chats every 60 s, and posts down and recovery notices to the operator DM.
- `social keepers` returns no kept seats on 2026-11-06. The kept seats from October (Tony's Discord DM, unimatrix0 #bots, Hallam general, muse) no longer have tables. #bots is held by job 30bbc7a3 on deneb, which mounted it by hand.
- Redux `channels.md` R72 item 5 keeps one long-lived holder on purpose: a persistent room, such as the muse two-way link or Caviar's stream listener.
- The new lifecycle keeps no agent alive while it is idle. That is the opposite of what the keeper does.

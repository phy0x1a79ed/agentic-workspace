# rt-teach

## Purpose & Contents

The handover of the new rt setup to the original RT agent, over a persistent room, once rooms are up after deneb's cutover. It holds who the student is and what the lesson covers.

## Student

Unknown. rt-container (job 39eba8f8, mizar) is not the original. No Claude transcript on mira or mizar mentions ibkr_day.py. rt's comments ("mac/polygon backtests", "miraz", wiki links) point to Tony's Mac, or to capella or lyra over ssh into mira.

## Lesson, in order (from rt-container, 2026-11-05)

1. Code lives in git: the private stock project (mizar, projects/stock) holds rt/ as a snapshot of mira's ~/rt (b9a1b02). The only code edit so far is IB_CLIENT_ID (c52f03a). The frames are DVC-tracked at data/frames. Edit in a stock scope, never on alcor.
2. rt on alcor is the stock repo's plugins/rt: system unit awm-rt, user rt, a compute job and no singleton. jobs.toml replaces mira's timers. Each job runs in bubblewrap with mira's paths. Jobs schedule in America/Los_Angeles.
3. Research code moves by `rt pin <commit>`, with no reload. A change to the shell lands by the stock deploy to alcor and a restart of awm-rt outside market hours.
4. Verbs: rt status, jobs, run, stop, logs, pin.
5. IBKR: stock-market is a hidden singleton. IB Gateway runs under IBC in rootless docker. The API's only door is /run/awm-stock-market/ibapi.sock, seen as 127.0.0.1:4002 inside the sandbox. Client ids are leased from pool 100–199, so scripts read IB_CLIENT_ID.
6. Rules: one IBKR login at a time, never print or commit secrets, Massive's free-tier rate limit.

## Who teaches

The agent on alcor teaches. The room opens once channel's persistent rooms pass the window check (deneb-retry).

## Student, settled

Tony talks with the rt stock session himself: "I will talk with rt stock session" (2026-11-05).
- The student is external: the original RT agent belongs to Tony's wife and runs on her own systems, outside the fleet (Tony, via rt-container, 2026-11-05). The room is the agent-to-agent link across the two setups. The student joins as the room's guest over the public relay link. It never appears in edges.yml, and it reaches the stock project only through what the room carries.

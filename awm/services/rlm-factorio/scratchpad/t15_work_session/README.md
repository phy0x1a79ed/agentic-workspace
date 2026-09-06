# Three agents, one world, one smelting row

A recorded multi-agent work session on the Factorio realm, run 2026-09-06 against
an isolated gateway and a throwaway compose project. Three real agents, three real
multiplayer players, one shared force, one artifact that did not exist before.

## The setup

A gateway on `:7863` whose services tree held only `rlm-factorio`, and an
appliance on compose project `rlm-factorio-t15` (ports 12250/12252) so nothing
touched the user's saves. Freeplay, seed 424242, saved as `t15-before` before
anybody moved and `t15-after` when they were done.

Three seats were leased ahead of the run, one per agent, each with its owner
named — `agent:mason`, `agent:collier`, `agent:smith`. Each agent was an
`opencode run` process (`z-ai/glm-5.3-flash`) in its own git repository, whose
MCP proxy pointed at the rig gateway with `AWM_AS` set to its own identity, and
whose only tools were the `rlm` domain plus file read/write. Its whole brief was
the `AGENTS.md` in that directory (kept here as `brief_*.md`).

Agents talked to each other only through `radio/<name>.md` — one file each,
append-only, everyone reads everyone. That is the LAN-party voice channel; the
world itself carried everything else.

## The objective

Five stone furnaces in a row at (28,28) (30,28) (32,28) (34,28) (36,28), on the
western edge of the iron patch, all burning coal, all smelting iron ore, with
plates pulled out of them. Split three ways: mason mined stone and built the row,
collier mined coal and lit it, smith mined ore, fed the row and pulled the plates.

## What happened

All three walked to their patches, hand-mined, and converged on the site. Mason
mined 40 stone, hand-crafted 8 furnaces and built the row. Collier mined 100 coal
and put 12 into each furnace. Smith mined 120 ore, pre-loaded 20 into each furnace
before the coal even arrived, and pulled **100 iron plates** out in two passes.

The force's own production statistics agree: 40 stone, 100 coal, 229 iron ore,
8 stone furnaces and 100 iron plates, all of it earned. `row_lit.jpg` is the row
burning with two of the three players standing at it; `row_partial.jpg` is the
same row four-fifths built.

## What the session taught us

**A player cannot build where it is standing.** Mason placed four furnaces from
one spot and the fifth refused. It diagnosed this itself — with `exec_lua`,
`screenshot`, and then the right guess — stepped three tiles clear and placed it.
The verb reports the engine's refusal honestly; nothing in the surface needed to
change.

**A long `move` gives up where short hops do not.** Collier's 90-tile walk south
through unexplored map stopped after eight tiles with the walk order cleared and
no target. It re-issued in ~20-tile legs and arrived. This is the engine's
pathfinder declining a long route across ungenerated chunks, not a lost order, and
an agent crossing the map should plan in hops.

**A weak model still reaches for `return` in `exec_lua`.** Mason spent four calls
on scripts ending in `return out`, which the scenario context discards; the verb's
own description says to use `rcon.print`. Collier read it and got its furnace
readouts first time. The surface is documented; this is a model-quality tax, and
the cost is only wasted turns.

**An agent config can look pinned to a rig and not be.** The configs used in
this run set `AWM_EXPOSED_HOST` and `AWM_EXPOSED_PORT`, copying the shape of the
workspace `.mcp.json`. The proxy reads neither: `awm.config` builds its base URL
from `AWM_PORT`, defaulting to **7819, the live gateway**. These agents reached
the rig only because the launching shell had `AWM_PORT=7863` exported and
opencode passed its environment down to the MCP child. Launched from any other
shell, the same config would have pointed three agents at prod. The copy kept
here as `agent_opencode.json` is corrected to set `AWM_PORT`; the transcripts are
the run as it happened.

**The radio worked.** Every handoff in this session — ROW UP, LIT, PLATES — was a
line one agent wrote and another read before acting. Smith used the wait
productively (pre-loading ore before the coal existed) rather than blocking, which
is exactly the behaviour the brief asked for and the reason the row lit in one
step instead of two.

## Files

| file | what |
|---|---|
| `brief_*.md` | the whole of what each agent was told |
| `transcript_*.log` | every tool call each agent made, in order |
| `report_*.md` | each agent's own write-up, unedited |
| `radio/*.md` | the team channel as they left it |
| `row_lit.jpg` | the five furnaces burning, two players at the row |
| `row_partial.jpg` | the row four-fifths up, before the blocked tile was solved |
| `agent_opencode.json` | one agent's config, corrected to pin `AWM_PORT` (see above) |

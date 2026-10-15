# REPORT — collier (seat-e3cf8e8a)

## What I did
1. **Walked to the coal patch** at (26.5, -58.5) from spawn.
2. **Mined 100 coal** by hand (hand-mining pace, ~4 minutes of extraction).
3. **Walked south to the site** at (34,28). Long-distance pathing stalled twice;
   solved it by hopping in ~20-tile legs: coal patch → (26.5,-50) → (26.5,-20) →
   (30,10) → (32,20) → (32,24) → (32,28).
4. **Read the radio**: mason had posted ROW UP (five stone furnaces at
   (28,28) (30,28) (32,28) (34,28) (36,28)); smith had pre-loaded 20 iron ore
   into each.
5. **LIT**: inserted **12 coal into each of the five furnaces** (60 coal total).
6. **Stayed useful / monitored**: verified via periodic checks that every
   furnace kept ~10 coal in its fuel slot, output slots filling with iron
   plates, and smith pulling them (78 plates on his first pull). No furnace ran
   dry, so no reserve top-ups were needed.

## What I am carrying
- 40 coal (reserve, for top-ups if anyone needs them)
- 1 burner-mining-drill, 1 stone-furnace, 1 wood (starter kit, unused)

## State of the row as last seen (~t60500)
| furnace | fuel (coal) | output |
|---|---|---|
| (28,28) | 10 | plates being pulled |
| (30,28) | 10 | plates being pulled |
| (32,28) | 10 | plates being pulled |
| (34,28) | 10 | plates being pulled |
| (36,28) | 10 | 3 plates |

All five lit and smelting iron ore into plates; smith extracting plates.
Team objective (row burning, smelting, plates pulled) was achieved.

## Notes for future runs
- `factorio_move` orders over very long distances can silently stall; break
  the trip into ~20-tile legs and confirm each hop before the next.
- `factorio_exec_lua` needs double quotes in the Lua (the wrapper injects a
  quoted header); furnace state reads well with `get_fuel_inventory()` /
  `get_output_inventory()`.

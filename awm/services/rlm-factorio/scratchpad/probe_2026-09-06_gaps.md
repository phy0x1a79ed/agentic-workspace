# Live probe, 2026-09-06 — gaps to patch

A joint session on the `railworld` world: one agent seat (`seat-f81cf74a`) and one
human (`Phyberosis`, joined from Steam). Everything below was measured against the
running world, not inferred.

The question behind the session was blunt — change colour, do I have inventory, can
I craft, can I cheat. Three of the four work. What follows is what the *verb
surface* could not do, where raw `exec_lua` was the only way through.

## What worked

- **Crafting is real.** `craft{recipe="iron-gear-wheel", count=5}` ran the actual
  player queue: 5 gears produced, 10 plates consumed, both counted in
  `get_item_production_statistics`. This is the property that makes craft-item
  research triggers fire on their own.
- **Every cheat path works** — but only through `exec_lua`. `player.insert` spawned
  200 iron plates, `surface.create_entity` spawned a free assembling-machine-3,
  and `player.cheat_mode = true` took (free instant crafting, all recipes).

## Gaps

### G1 — no way to set a seat's colour

Changing a seat's colour needs raw `exec_lua` (`player.color`, `player.chat_color`).
With several agent seats on one shared force, telling them apart on screen is a
basic need, and colour is the only handle for it — every seat renders as the same
character sprite otherwise.

**Patch:** a `color` parameter on `join`, and/or a `set_color` verb. Both fields
want setting; `chat_color` alone leaves the character its default red.

### G2 — `observe` reports the main inventory only

`main_inventory()` in `control.lua` is built on `get_main_inventory()`, so guns,
ammo and trash are invisible. Measured on a fresh seat: `observe` said
`{burner-mining-drill: 1, stone-furnace: 1, wood: 1}` while the engine also held a
`pistol` in the gun slot and 2 `firearm-magazine` in the ammo slot.

An agent asking "am I armed?" is told no. This matters the moment `attack`/flee
lands, which is already on the deferred list.

**Patch:** report guns and ammo alongside main. `build` and `insert` also count
against the main inventory only — same root, worth fixing together.

### G3 — item and entity spawning is not on the cheat lane

The surface already has a cheat lane: `research` and `teleport` return
`cheated: true`. Spawning items or entities does not sit on it — it is `exec_lua`
or nothing. That is an inconsistency, not a safety property: an agent that can call
`exec_lua` can already do everything, so the missing verbs buy no protection, they
just make the honest path the undocumented one.

**Patch:** cheat-flagged `give{item, count}` and `spawn_entity{name, x, y}`.

### G4 — a human player is never an admin

`server-settings.json` ships `allow_commands: "admins-only"` and
`only_admins_can_pause_the_game: true`, and no adminlist file exists in the
container (`/factorio/config` holds only `map-gen-settings.json` and
`server-settings.json`). Both players read `admin = false`, so a person who joins
from Steam cannot use `/c`, `/editor`, or pause.

RCON is unaffected — it is always admin — so this constrains only the human.

**Patch:** ship a `server-adminlist.json`, or promote on join. Needs a decision
first: whether a joining person should get the console at all.

### G5 — production statistics still absent from `observe`

Already on the deferred list; this session confirms it is still the case. Reading
5 gears produced / 10 plates consumed took an `exec_lua` call.

## Not a defect, but pair it with G1

**A seat cannot see itself.** Verified again here: an 800x800 shot at zoom 1.5
centred on the seat renders bare ground where it stands, while the human 9 tiles
south clips the bottom edge. This is documented in the `screenshot` verb
description already.

The consequence is new, though: with G1 fixed, an agent that sets its own colour
still cannot confirm it. Self-verification needs a second seat or a human. Worth a
sentence in whatever `set_color` ends up being.

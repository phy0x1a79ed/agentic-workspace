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

---

# Second pass — the surface as a whole

The first pass listed things that did not work. This pass asks whether the verbs
are the right *set*. Findings are numbered on from G5.

### G6 — `observe` calls every player a "seat"

`observe`'s `players` list and its `nearby` scan both label a player with the key
`seat`, holding a player *name*. A human who joined from Steam has no seat — no
row, no lease, no owner, and the reaper structurally cannot touch them. Measured
live: `seats` returns one row (the agent's) while `observe` reports
`{"seat": "Phyberosis", ...}` for the human.

Not exploitable — addressing a verb at `"Phyberosis"` fails as an unknown seat —
but it is the wrong word in the one place the distinction carries weight. An agent
cannot tell a teammate it can coordinate with from a person it cannot.

**Patch:** rename the key to `player`, and add a boolean `seat` (or a `seat_id`
that is null for a human).

### G7 — `mine` is two operations under one name

`mine` filters on `mineable_properties.minable`, not on resource type, so it also
deconstructs: verified by picking a spawned `assembling-machine-3` up off the
ground and into the seat's inventory. Its description says "resource/tree/rock",
which names only half of what it does.

So deconstruct is NOT a missing primitive — it is an undocumented one. **Patch is
the description**, not the code. Worth deciding whether harvest and deconstruct
should stay fused; they are one engine operation but two intents, and an agent
that means to mine ore near its own machines can eat them by accident.

### G8 — placement checking is fused into the attempt

`build` calls `can_place_entity{build_check_type = manual}` internally and
`blueprint_stamp` pre-counts obstructions and reports `blocked` / `blocked_at`.
Both fold the question into the action. There is no way to *ask* — no predicate
that answers "can this go here" without trying.

**Patch:** one `can_place` verb answering for a single entity and a blueprint
alike (same question, different cardinality), returning the `blocked_at` shape
`blueprint_stamp` already produces. `blueprint_stamp`'s pre-count should then call
it rather than carry a second implementation.

### G9 — nothing configures an entity after it is placed

`build` takes `direction` at placement and that is the only moment any property of
an entity can be set. There is no rotate, no set-recipe, no inserter filter, no
chest limit, no circuit wiring.

Rotate is the member of this family that gets noticed first. **Set-recipe is the
one that matters** — without it an assembler placed by an agent can never do
anything, so the surface stops exactly where automation begins.

**Patch:** this is a missing *category*, not a missing verb. The engine models all
of it as properties on a `LuaEntity`, so one `configure{x, y, ...}` verb buys
rotate, recipe, filters and limits together. Adding `rotate` alone patches the
symptom and leaves the category open.

### G10 — no structured read of what is on screen

`screenshot` returns pixels; `observe` returns a capped nearby summary carrying
only name/type/position. Between them there is no way to enumerate what is in
view, and nothing anywhere reports how entities are **connected**. An agent can
see a factory and cannot read it.

Prototyped live (`scratchpad/` probe, not committed as code) and it works. For a
frame around a seat it returned 18 entities and an 8-edge graph:

- per entity: `id` (unit_number), name, type, position, `direction`, `status`,
  `recipe`, `contents`, `enet` (electric network id), damaged health.
- per edge: `{from, to, kind}` with kinds `belt`, `inserter_pickup`,
  `inserter_drop`, `circuit_red`, `circuit_green`.

`status` is the payload that matters. The probe's assembler read
`item_ingredient_shortage` while its inserters read `waiting_for_source_items`,
and the edge list said why: the input inserter was picking up FROM the assembler
and dropping INTO the supply chest — built facing the wrong way. That diagnosis is
unavailable from a screenshot and unavailable from `observe`.

**Shape.** A verb of its own named `list_fov`, paired explicitly with
`screenshot`: one field of view, two media. Both take the SAME FOV block (`x`,
`y`, `width`, `height`, `zoom`, `surface`), so rendering a box and enumerating it
are provably the same box. `observe` stays what it is: seat-centric, small, cheap.

**Bounding is mandatory.** A real factory is thousands of entities. Resources must
aggregate by name rather than list per tile (the probe does this; the iron patch
alone is 2414 tiles). Needs `limit` + `truncated` like `recipes`/`technologies`,
and probably a `types` filter.

**Four things the probe got wrong, to fix in a real implementation:**

1. **No `power_copper` edges.** Poles reported a shared `enet` but
   `entity.neighbours` yielded nothing under `pcall` — the failure was swallowed
   rather than surfaced. Needs the 2.0 wire-connector API, not `neighbours`.
2. **Undirected edges appear twice.** The red circuit wire came back as both
   pole A -> B and B -> A. Symmetric kinds need dedup; belt/inserter kinds are
   genuinely directed and must not be.
3. **Empty lists encode as `{}`.** `resources` came back an object, not an array
   — the same Lua one-table-type problem T8 already fixed for `observe`'s lists.
   Coerce service-side.
4. An unpaired `underground-belt` reports no partner edge. Pairs
   (`neighbours` for undergrounds, and loaders) need their own kind.

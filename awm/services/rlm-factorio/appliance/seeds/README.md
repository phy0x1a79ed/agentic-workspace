# Seed maps

Pre-generated Factorio save files kept here as **durable seeds** — they live in
the repo, not in the `factorio-saves` Docker volume, so `world_new` / `world_load`
/ a volume wipe can never overwrite them. Copy one into a session's saves volume
to start a fresh world from it.

## `railworld-rich.zip`

A rail-world-style Nauvis map with **massive, very rich** resource patches.

- Generated from `railworld-rich.map-gen-settings.json` (resources spread out like
  a rail world — low frequency — but each patch is `size 6` / `richness 6`) and
  `railworld-rich.map-settings.json` (full default tree with **enemy expansion
  disabled** — the rail-world signature — and **enemy evolution toned down**:
  time-based evolution disabled (`time_factor = 0`), with the pollution- and
  kill-based factors cut to 20% of default (`destroy_factor 0.002 → 0.0004`,
  `pollution_factor 9e-07 → 1.8e-07`)). Seed `1234567`, starting area `2` (big).
- Baked on the **2.1.8** appliance image **with the `game-bot-control` mod loaded
  at map-gen**, so the agent player-body works the instant you load it (no
  `world_new` needed first).
- Measured within 256 tiles of spawn: ~29.5M iron, ~36.9M coal, ~9.0M copper,
  ~2.3M stone (≈12k ore/tile vs. a vanilla patch's few hundred).

The two `.json` files are the recipe — kept alongside the `.zip` so the map can be
regenerated or tweaked; they are **not** read at runtime.

## Copy a seed in to start a new world

The realm's `world_load <name>` re-execs the engine on a copy of `<name>.zip` from
the session's saves volume, leaving the named save read-only (sacred-saves). So:
drop the seed into the volume under a name, then `world_load` it.

```bash
# With a session acquired (container up). Find the container:
CID="$(docker compose -p rlm-factorio ps -q factorio)"

# Copy the seed in as a named save, then load it via the realm verb:
docker cp seeds/railworld-rich.zip "$CID:/factorio/saves/railworld.zip"
#   then:  rlm_factorio_world_load { session_id, name: "railworld" }
# or against the supervisor control surface directly:
#   curl -X POST http://127.0.0.1:12142/load -d '{"name":"railworld"}'
```

`world_load` runs the engine on the `_active` scratch copy, so playing never
mutates `railworld.zip`; reload it any time to reset to the pristine seed.

## Regenerate / make another seed

```bash
docker run --rm -v "$PWD/seeds:/gen" \
  --entrypoint /opt/factorio/bin/x64/factorio \
  rlm-factorio/appliance:2.1.8 \
  --create /gen/railworld-rich.zip \
  --map-gen-settings /gen/railworld-rich.map-gen-settings.json \
  --map-settings    /gen/railworld-rich.map-settings.json \
  --mod-directory /opt/factorio/mods
```

Keep `--mod-directory /opt/factorio/mods` — it's what binds the player-body mod at
map-gen. `--map-gen-settings` may be sparse; `--map-settings` must be a **complete**
tree (start from `/opt/factorio/data/map-settings.example.json` in the image).

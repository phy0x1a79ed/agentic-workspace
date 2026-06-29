-- game-bot-control: a script-controlled character body for agent play.
--
-- There is no LuaPlayer on a headless server, so the "body" is a bare
-- `character` entity. A bare character has no controller, but its
-- `walking_state` PERSISTS across ticks (empirically: set once, it keeps
-- walking ~0.15 tiles/tick on its own). So the on_tick driver only STEERS and
-- STOPS -- it re-issues walking_state only when the heading changes, and clears
-- it on arrival. It never scans entities (observe does that, on demand).
--
-- All mutable state lives in `storage` (the 2.0 rename of `global`), which
-- serializes into the save -- so the body and its target survive new/load/save
-- for free. Every body access is guarded with `.valid`.

local DEFAULT_SURFACE = "nauvis"
local ARRIVE_DIST = 0.2          -- tiles; within this we consider the body arrived
local EVENTS_CAP = 64            -- bounded ring buffer for transient events
local NEARBY_CAP = 50            -- cap observe's entity list so the RCON payload stays bounded
local DEFAULT_RADIUS = 32

-- 8 headings stepping clockwise from east. Factorio's y grows DOWNWARD, so
-- atan2(dy, dx) == 0 is east and +pi/2 is south. defines.direction is 16-way in
-- 2.0; these eight cardinal/diagonal members are the every-other values.
local DIRS = {
  defines.direction.east,
  defines.direction.southeast,
  defines.direction.south,
  defines.direction.southwest,
  defines.direction.west,
  defines.direction.northwest,
  defines.direction.north,
  defines.direction.northeast,
}

local function init_state()
  if not storage.bot then
    storage.bot = { body = nil, target = nil, last_dir = nil, events = {} }
  end
end

script.on_init(init_state)
script.on_configuration_changed(init_state)

local function push_event(kind, data)
  local ev = storage.bot.events
  ev[#ev + 1] = { kind = kind, tick = game.tick, data = data }
  while #ev > EVENTS_CAP do table.remove(ev, 1) end
end

local function heading(from, to)
  local ang = math.atan2(to.y - from.y, to.x - from.x)   -- east=0, south=+pi/2
  if ang < 0 then ang = ang + 2 * math.pi end
  local sector = math.floor((ang + math.pi / 8) / (math.pi / 4)) % 8
  return DIRS[sector + 1]
end

-- on_tick: steer toward target, stop on arrival. Cheap -- no entity scans.
script.on_event(defines.events.on_tick, function()
  local b = storage.bot
  if not (b and b.target and b.body and b.body.valid) then return end
  local body = b.body
  local pos = body.position
  local dx, dy = b.target.x - pos.x, b.target.y - pos.y
  if (dx * dx + dy * dy) <= (ARRIVE_DIST * ARRIVE_DIST) then
    body.walking_state = { walking = false }
    b.target = nil
    b.last_dir = nil
    push_event("arrived", { x = pos.x, y = pos.y })
    return
  end
  local d = heading(pos, b.target)
  if d ~= b.last_dir then
    body.walking_state = { walking = true, direction = d }
    b.last_dir = d
  end
end)

-- inventory contents, normalized to a {name = count} map across the 2.0
-- get_contents() array format (and the legacy map, defensively).
local function main_inventory(body)
  local inv = body.get_main_inventory()
  if not inv then return nil end
  local out = {}
  for k, v in pairs(inv.get_contents()) do
    if type(v) == "table" then            -- 2.0: array of {name, count, quality}
      out[v.name] = (out[v.name] or 0) + v.count
    else                                  -- legacy: {name = count}
      out[k] = v
    end
  end
  return out
end

remote.add_interface("game_bot", {
  spawn = function(args)
    args = args or {}
    if storage.bot.body and storage.bot.body.valid then
      return { ok = true, existing = true,
               position = storage.bot.body.position,
               surface = storage.bot.body.surface.name }
    end
    local surf = game.surfaces[args.surface or DEFAULT_SURFACE]
            or game.surfaces[DEFAULT_SURFACE]
    local pos = { x = args.x or 0, y = args.y or 0 }
    local body = surf.create_entity{ name = "character", position = pos, force = "player" }
    if not body then error("failed to create character entity") end
    storage.bot.body = body
    storage.bot.target = nil
    storage.bot.last_dir = nil
    push_event("spawned", { x = body.position.x, y = body.position.y, surface = surf.name })
    return { ok = true, position = body.position, surface = surf.name }
  end,

  despawn = function()
    if storage.bot.body and storage.bot.body.valid then
      storage.bot.body.destroy()
    end
    storage.bot.body = nil
    storage.bot.target = nil
    storage.bot.last_dir = nil
    return { ok = true }
  end,

  set_target = function(args)
    if not (storage.bot.body and storage.bot.body.valid) then
      error("no body -- spawn first")
    end
    storage.bot.target = { x = args.x, y = args.y }
    storage.bot.last_dir = nil
    return { ok = true, target = storage.bot.target }
  end,

  stop = function()
    storage.bot.target = nil
    storage.bot.last_dir = nil
    if storage.bot.body and storage.bot.body.valid then
      storage.bot.body.walking_state = { walking = false }
    end
    return { ok = true }
  end,

  observe = function(args)
    args = args or {}
    local b = storage.bot
    local out = { tick = game.tick, paused = game.tick_paused, body = false }
    if b.body and b.body.valid then
      local body = b.body
      local ws = body.walking_state
      out.body = true
      out.surface = body.surface.name
      out.position = body.position
      out.health = body.health
      out.max_health = body.max_health
      out.walking = ws and ws.walking or false
      out.target = b.target
      out.inventory = main_inventory(body)
      local radius = args.radius or DEFAULT_RADIUS
      local near = {}
      for _, ent in pairs(body.surface.find_entities_filtered{
            position = body.position, radius = radius }) do
        if ent ~= body and ent.valid then
          near[#near + 1] = { name = ent.name, type = ent.type,
                              x = ent.position.x, y = ent.position.y }
          if #near >= NEARBY_CAP then break end
        end
      end
      out.nearby = near
    end
    return out
  end,
})

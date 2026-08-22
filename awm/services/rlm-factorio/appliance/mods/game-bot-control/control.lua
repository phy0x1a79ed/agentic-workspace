-- game-bot-control: a script-controlled character body for agent play.
--
-- There is no LuaPlayer on a headless server, so the "body" is a bare
-- `character` entity. A bare character has no controller, but its
-- `walking_state` PERSISTS across ticks (empirically: set once, it keeps
-- walking ~0.15 tiles/tick on its own). So the on_tick driver only STEERS and
-- STOPS -- it re-issues walking_state only when the heading changes, and clears
-- it on arrival. It never scans entities (observe does that, on demand).
--
-- Movement is PATHFINDED: set_target asks the engine for a route
-- (surface.request_path) and the on_tick driver walks the waypoints, so the
-- body no longer wedges on the first obstacle. A near goal falls back to the
-- old straight steer when no path exists; stuck-detection re-paths once, then
-- reports path_blocked. Path request ids do NOT survive save/load or an engine
-- re-exec, so an unanswered request is re-issued after a timeout.
--
-- All mutable state lives in `storage` (the 2.0 rename of `global`), which
-- serializes into the save -- so the body and its target survive new/load/save
-- for free. Every body access is guarded with `.valid`.

local DEFAULT_SURFACE = "nauvis"
local ARRIVE_DIST = 0.2          -- tiles; within this we consider the body arrived
local WP_ARRIVE = 0.5            -- tiles; close enough to an intermediate waypoint
local PATH_RETRY_TICKS = 300     -- unanswered path request -> re-issue (covers save/load orphans)
local STUCK_TICKS = 60           -- ticks without progress before re-path / give up
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

local function sqdist(a, b)
  local dx, dy = a.x - b.x, a.y - b.y
  return dx * dx + dy * dy
end

-- Forget everything about the current move (target, waypoints, pending path
-- request, stuck bookkeeping). The body's walking_state is the caller's call.
local function clear_motion(b)
  b.target = nil
  b.last_dir = nil
  b.waypoints = nil
  b.wp_i = nil
  b.path_req = nil
  b.path_wait = nil
  b.stuck = nil
  b.last_pos = nil
  b.repathed = nil
end

-- Ask the engine for a route to b.target (async; answered by
-- on_script_path_request_finished). Uses the character's own footprint +
-- collision mask so the route is walkable for *this* body.
local function request_path(b)
  local body = b.body
  b.waypoints, b.wp_i = nil, nil
  b.path_wait = 0
  b.path_req = body.surface.request_path{
    bounding_box = body.prototype.collision_box,
    collision_mask = body.prototype.collision_mask,
    start = body.position,
    goal = b.target,
    force = body.force,
    radius = 0.5,
    entity_to_ignore = body,
    can_open_gates = true,
    pathfind_flags = { cache = false, prefer_straight_paths = true },
  }
end

script.on_event(defines.events.on_script_path_request_finished, function(e)
  local b = storage.bot
  if not (b and b.path_req and e.id == b.path_req) then return end
  b.path_req = nil
  b.path_wait = nil
  if not (b.body and b.body.valid and b.target) then return end
  if e.try_again_later then
    request_path(b)                      -- pathfinder saturated; just re-ask
    return
  end
  if e.path then
    local wps = {}
    for i, wp in ipairs(e.path) do
      wps[i] = { x = wp.position.x, y = wp.position.y }
    end
    b.waypoints = wps
    b.wp_i = 1
  elseif sqdist(b.body.position, b.target) <= 8 * 8 then
    -- No route but the goal is near: fall back to the straight steer (the
    -- pathfinder refuses goals inside collision boxes that a direct approach
    -- can still get within reach of).
    b.waypoints = nil
  else
    local tx, ty = b.target.x, b.target.y
    b.body.walking_state = { walking = false }
    clear_motion(b)
    push_event("path_blocked", { x = tx, y = ty, reason = "no_path" })
  end
end)

-- on_tick: follow the waypoint chain (or straight-steer as fallback), stop on
-- arrival. Cheap -- no entity scans; walking_state is re-issued only when the
-- heading changes.
script.on_event(defines.events.on_tick, function()
  local b = storage.bot
  if not (b and b.target and b.body and b.body.valid) then return end
  local body = b.body
  local pos = body.position

  -- Waiting on the pathfinder: hold position. Re-issue if the answer never
  -- comes -- request ids don't survive save/load or an engine re-exec.
  if b.path_req then
    b.path_wait = (b.path_wait or 0) + 1
    if b.path_wait > PATH_RETRY_TICKS then request_path(b) end
    return
  end

  -- Arrived at the actual target?
  if sqdist(pos, b.target) <= ARRIVE_DIST * ARRIVE_DIST then
    body.walking_state = { walking = false }
    clear_motion(b)
    push_event("arrived", { x = pos.x, y = pos.y })
    return
  end

  -- Stuck detection: no movement while trying to walk -> re-path once; if
  -- that also wedges, close-enough counts as arrival, otherwise give up.
  if b.last_pos and sqdist(pos, b.last_pos) < 0.0004 then
    b.stuck = (b.stuck or 0) + 1
  else
    b.stuck = 0
    b.last_pos = { x = pos.x, y = pos.y }
  end
  if (b.stuck or 0) >= STUCK_TICKS then
    if sqdist(pos, b.target) <= 1.5 * 1.5 then
      body.walking_state = { walking = false }
      clear_motion(b)
      push_event("arrived", { x = pos.x, y = pos.y, inexact = true })
    elseif not b.repathed then
      b.repathed = true
      b.stuck = 0
      request_path(b)
    else
      local tx, ty = b.target.x, b.target.y
      body.walking_state = { walking = false }
      clear_motion(b)
      push_event("path_blocked", { x = tx, y = ty, reason = "stuck" })
    end
    return
  end

  -- Pick the current goal: the next waypoint, or the target itself on the
  -- final leg (the last waypoint only approximates the goal by `radius`).
  local goal = b.target
  if b.waypoints and b.wp_i then
    while b.wp_i <= #b.waypoints
          and sqdist(pos, b.waypoints[b.wp_i]) <= WP_ARRIVE * WP_ARRIVE do
      b.wp_i = b.wp_i + 1
      b.repathed = nil               -- progress made; allow a future re-path
    end
    if b.wp_i <= #b.waypoints then goal = b.waypoints[b.wp_i] end
  end

  local d = heading(pos, goal)
  if d ~= b.last_dir then
    body.walking_state = { walking = true, direction = d }
    b.last_dir = d
  end
end)

-- Body death: surface it as an event and forget the reference, so observe
-- reports body=false and the agent knows to respawn. Filtered to characters,
-- so other deaths never wake the handler.
script.on_event(defines.events.on_entity_died, function(e)
  local b = storage.bot
  if not (b and b.body and b.body.valid) then return end
  if e.entity == b.body then
    push_event("died", { x = e.entity.position.x, y = e.entity.position.y,
                         cause = e.cause and e.cause.valid and e.cause.name or nil })
    b.body = nil
    clear_motion(b)
  end
end, {{ filter = "type", type = "character" }})

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

-- ---- gameplay helpers ------------------------------------------------------

local function body_or_error()
  local b = storage.bot
  if not (b and b.body and b.body.valid) then error("no body -- spawn first") end
  return b.body
end

-- All interactions are reach-gated so the agent must actually walk to things
-- (no teleport-mining across the map).
local function check_reach(body, pos)
  local reach = (body.reach_distance or 8) + 0.5
  if sqdist(body.position, pos) > reach * reach then
    error(string.format(
      "out of reach: (%.1f,%.1f) is %.1f tiles away (reach %.1f) -- move closer",
      pos.x, pos.y, math.sqrt(sqdist(body.position, pos)), reach))
  end
end

-- The closest interactable entity within ~1.5 tiles of a point (machines,
-- chests, ... -- never the body, other characters, or raw resources).
local function find_target(body, pos, name)
  local best, bestd
  for _, ent in pairs(body.surface.find_entities_filtered{
        position = pos, radius = 1.5, name = name }) do
    if ent.valid and ent ~= body
       and ent.type ~= "character" and ent.type ~= "resource" then
      local d = sqdist(ent.position, pos)
      if not bestd or d < bestd then best, bestd = ent, d end
    end
  end
  return best
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
    clear_motion(storage.bot)
    push_event("spawned", { x = body.position.x, y = body.position.y, surface = surf.name })
    return { ok = true, position = body.position, surface = surf.name }
  end,

  despawn = function()
    if storage.bot.body and storage.bot.body.valid then
      storage.bot.body.destroy()
    end
    storage.bot.body = nil
    clear_motion(storage.bot)
    return { ok = true }
  end,

  set_target = function(args)
    body_or_error()
    local b = storage.bot
    clear_motion(b)
    b.target = { x = args.x, y = args.y }
    request_path(b)
    return { ok = true, target = b.target, pathfinding = true }
  end,

  stop = function()
    local b = storage.bot
    clear_motion(b)
    if b.body and b.body.valid then
      b.body.walking_state = { walking = false }
    end
    return { ok = true }
  end,

  -- ---- gameplay actions ----
  --
  -- `mining_state` self-clears on a controller-less character, so `mine` is
  -- SCRIPTED: yield the products into the inventory and decrement/destroy the
  -- entity. `begin_crafting` and `create_entity` are the two engine paths that
  -- genuinely work for a bare character (probed live), so craft/build ride
  -- them directly.

  mine = function(args)
    args = args or {}
    local body = body_or_error()
    if args.x == nil or args.y == nil then error("mine requires x and y") end
    local pos = { x = args.x, y = args.y }
    check_reach(body, pos)
    local target, bestd
    for _, ent in pairs(body.surface.find_entities_filtered{
          position = pos, radius = 1.5, name = args.name }) do
      if ent.valid and ent ~= body and ent.type ~= "character"
         and ent.prototype.mineable_properties
         and ent.prototype.mineable_properties.minable then
        local d = sqdist(ent.position, pos)
        if not bestd or d < bestd then target, bestd = ent, d end
      end
    end
    if not target then
      error(string.format("nothing minable at (%.1f,%.1f)", pos.x, pos.y))
    end
    local props = target.prototype.mineable_properties
    if props.required_fluid then
      error(target.name .. " requires " .. props.required_fluid .. " to mine")
    end
    local inv = body.get_main_inventory()
    if not inv then error("body has no inventory") end
    local mined = {}
    if target.type == "resource" then
      -- Per-unit yield: insert first, then consume only what actually fit,
      -- so a full inventory never destroys ore.
      local units = math.min(args.count or 1, target.amount)
      if units < 1 then error("resource is exhausted") end
      local consumed = 0
      for _, p in pairs(props.products or {}) do
        if p.type == "item" then
          local per = p.amount or p.amount_max or 1
          local got = inv.insert{ name = p.name, count = units * per }
          if got > 0 then
            mined[p.name] = (mined[p.name] or 0) + got
            local used = math.ceil(got / per)
            if used > consumed then consumed = used end
          end
        end
      end
      if consumed == 0 then error("inventory full") end
      local left = target.amount - consumed
      if left > 0 then target.amount = left else target.destroy(); left = 0 end
      return { ok = true, mined = mined, remaining = left }
    else
      -- One-shot minable (tree / rock): destroy once, yield its products.
      for _, p in pairs(props.products or {}) do
        if p.type == "item" then
          local got = inv.insert{ name = p.name, count = p.amount or p.amount_max or 1 }
          if got > 0 then mined[p.name] = (mined[p.name] or 0) + got end
        end
      end
      if next(mined) == nil then error("inventory full") end
      local name = target.name
      target.destroy()
      return { ok = true, mined = mined, remaining = 0, removed = name }
    end
  end,

  craft = function(args)
    args = args or {}
    local body = body_or_error()
    if not args.recipe then error("craft requires 'recipe'") end
    local rec = body.force.recipes[args.recipe]
    if not rec then error("unknown recipe: " .. args.recipe) end
    if not rec.enabled then error("recipe not unlocked: " .. args.recipe) end
    local queued = body.begin_crafting{ recipe = args.recipe,
                                        count = args.count or 1 }
    if queued == 0 then
      error("cannot craft " .. args.recipe .. " (missing ingredients?)")
    end
    return { ok = true, queued = queued }
  end,

  build = function(args)
    args = args or {}
    local body = body_or_error()
    if not args.name then error("build requires 'name' (the item to place)") end
    if args.x == nil or args.y == nil then error("build requires x and y") end
    local pos = { x = args.x, y = args.y }
    check_reach(body, pos)
    local inv = body.get_main_inventory()
    if not inv or inv.get_item_count(args.name) < 1 then
      error("no " .. args.name .. " in inventory")
    end
    local item = prototypes.item[args.name]
    if not item or not item.place_result then
      error(args.name .. " is not placeable")
    end
    local dir = defines.direction.north
    if args.direction then
      dir = defines.direction[args.direction]
      if dir == nil then error("bad direction: " .. tostring(args.direction)) end
    end
    local ename = item.place_result.name
    if not body.surface.can_place_entity{
          name = ename, position = pos, direction = dir, force = body.force,
          build_check_type = defines.build_check_type.manual } then
      error(string.format("cannot place %s at (%.1f,%.1f) -- collision or invalid",
                          ename, pos.x, pos.y))
    end
    local ent = body.surface.create_entity{
      name = ename, position = pos, direction = dir, force = body.force,
      raise_built = true }
    if not ent then error("placement failed") end
    inv.remove{ name = args.name, count = 1 }   -- only after success
    return { ok = true, built = ename, position = ent.position }
  end,

  insert = function(args)
    args = args or {}
    local body = body_or_error()
    if not args.name then error("insert requires 'name'") end
    if args.x == nil or args.y == nil then
      error("insert requires x and y (the target entity)")
    end
    local pos = { x = args.x, y = args.y }
    check_reach(body, pos)
    local target = find_target(body, pos, args.target)
    if not target then
      error(string.format("no entity at (%.1f,%.1f)", pos.x, pos.y))
    end
    local inv = body.get_main_inventory()
    local have = inv and inv.get_item_count(args.name) or 0
    if have < 1 then error("no " .. args.name .. " in inventory") end
    -- entity.insert routes to the right slot (furnace fuel vs input, etc.)
    local put = target.insert{ name = args.name,
                               count = math.min(args.count or have, have) }
    if put == 0 then error(target.name .. " did not accept " .. args.name) end
    inv.remove{ name = args.name, count = put }
    return { ok = true, inserted = put, target = target.name }
  end,

  take = function(args)
    args = args or {}
    local body = body_or_error()
    if not args.name then error("take requires 'name'") end
    if args.x == nil or args.y == nil then
      error("take requires x and y (the source entity)")
    end
    local pos = { x = args.x, y = args.y }
    check_reach(body, pos)
    local target = find_target(body, pos, args.target)
    if not target then
      error(string.format("no entity at (%.1f,%.1f)", pos.x, pos.y))
    end
    local inv = body.get_main_inventory()
    if not inv then error("body has no inventory") end
    local avail = target.get_item_count(args.name)
    if avail < 1 then error(target.name .. " has no " .. args.name) end
    local got = target.remove_item{ name = args.name,
                                    count = math.min(args.count or avail, avail) }
    local kept = inv.insert{ name = args.name, count = got }
    if kept < got then
      target.insert{ name = args.name, count = got - kept }  -- overflow back
    end
    if kept == 0 then error("inventory full") end
    return { ok = true, taken = kept, from = target.name }
  end,

  research = function(args)
    args = args or {}
    if not args.name then error("research requires 'name'") end
    local force = game.forces.player
    local tech = force.technologies[args.name]
    if not tech then error("unknown technology: " .. args.name) end
    if tech.researched then
      return { ok = true, researched = args.name, already = true, cheated = false }
    end
    -- Legit research needs a powered-lab + science-pack chain the bot can't
    -- assemble yet, and script-crafted items never fire trigger-tech counters
    -- (probed live) -- so this flips the flag directly and SAYS SO.
    tech.researched = true
    push_event("researched", { name = args.name, cheated = true })
    return { ok = true, researched = args.name, cheated = true }
  end,

  -- ---- catalog queries (bounded: filter + cap, the RCON channel fragments
  -- past 4 KB) ----

  recipes = function(args)
    args = args or {}
    local b = storage.bot
    local force = (b.body and b.body.valid) and b.body.force or game.forces.player
    local limit = args.limit or 40
    local out = {}
    for name, rec in pairs(force.recipes) do
      if rec.enabled and not rec.hidden
         and (not args.search or string.find(name, args.search, 1, true)) then
        local ing, prod = {}, {}
        for _, i in pairs(rec.ingredients) do
          ing[#ing + 1] = { name = i.name, amount = i.amount, type = i.type }
        end
        for _, p in pairs(rec.products) do
          prod[#prod + 1] = { name = p.name, amount = p.amount or p.amount_max,
                              type = p.type }
        end
        out[#out + 1] = { name = name, ingredients = ing, products = prod }
        if #out >= limit then break end
      end
    end
    return { recipes = out, truncated = #out >= limit }
  end,

  technologies = function(args)
    args = args or {}
    local force = game.forces.player
    local limit = args.limit or 40
    local out = {}
    for name, tech in pairs(force.technologies) do
      if tech.enabled
         and (not args.search or string.find(name, args.search, 1, true))
         and (not args.only_unresearched or not tech.researched) then
        local prereq = {}
        for pname in pairs(tech.prerequisites) do
          prereq[#prereq + 1] = pname
        end
        out[#out + 1] = { name = name, researched = tech.researched,
                          prerequisites = prereq }
        if #out >= limit then break end
      end
    end
    return { technologies = out, truncated = #out >= limit }
  end,

  -- Return-and-clear in ONE call so no event can slip between read and reset;
  -- the supervisor's poller re-emits these up the hub as rlm.factorio events.
  drain_events = function()
    local ev = storage.bot.events
    storage.bot.events = {}
    return { events = ev }
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
      out.reach = body.reach_distance
      local q = {}
      for _, item in pairs(body.crafting_queue or {}) do
        q[#q + 1] = { recipe = item.recipe, count = item.count }
      end
      out.crafting = q
      local radius = args.radius or DEFAULT_RADIUS
      local near = {}
      for _, ent in pairs(body.surface.find_entities_filtered{
            position = body.position, radius = radius }) do
        if ent ~= body and ent.valid then
          local rec = { name = ent.name, type = ent.type,
                        x = ent.position.x, y = ent.position.y }
          if ent.type == "resource" then rec.amount = ent.amount end
          near[#near + 1] = rec
          if #near >= NEARBY_CAP then break end
        end
      end
      out.nearby = near
    end
    return out
  end,
})

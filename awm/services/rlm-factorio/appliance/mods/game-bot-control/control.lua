-- game-bot-control: per-seat control of REAL multiplayer players.
--
-- A seat is a full Factorio client that joined this world and minted a real
-- LuaPlayer; this mod drives those players. That is a different animal from the
-- bare `character` entity this mod used to puppet, and two engine rules follow
-- from it:
--
--  * INPUT IS PER-TICK. A real player's `walking_state` and `mining_state` are
--    an input frame: the engine discards them unless they are re-asserted every
--    tick. (A controller-less character was the opposite -- it kept walking from
--    a single assignment.) So the tick driver re-issues state every tick for
--    every active seat, and a "one-shot" call here only records intent.
--  * MINING AND CRAFTING ARE REAL. Held per-tick, `mining_state` yields ore
--    into the player's own inventory at the game's own rate, and
--    `begin_crafting` is counted by production statistics -- which is what makes
--    craft-item research triggers fire on their own. Nothing here decrements
--    `resource.amount` by script any more. Mining also needs
--    `update_selected_entity` re-asserted every tick: the engine mines the
--    SELECTION, so mining_state on its own is accepted and mines nothing.
--
-- `player.character` is nil whenever a player is disconnected (and briefly
-- while dead), so every access is guarded rather than cached.
--
-- All mutable state lives in `storage` (the 2.0 rename of `global`), which
-- serializes into the save, so seats survive save/load and an engine re-exec.
-- Seat state is keyed by player index and is pure bookkeeping: the players
-- themselves belong to the engine, so a seat entry is disposable.

local DEFAULT_SURFACE = "nauvis"
local ARRIVE_DIST = 0.2          -- tiles; within this we consider the seat arrived
local WP_ARRIVE = 0.5            -- tiles; close enough to an intermediate waypoint
local PATH_RETRY_TICKS = 300     -- unanswered path request -> re-issue (covers save/load orphans)
local STUCK_TICKS = 60           -- ticks without progress before re-path / give up
local MINE_TIMEOUT_TICKS = 3600  -- a mining order that yields nothing for a minute is done
local EVENTS_CAP = 64            -- bounded ring buffer for transient events
local NEARBY_CAP = 50            -- cap observe's entity list so the RCON payload stays bounded
local DEFAULT_RADIUS = 32
local ORDER_KEEP_TICKS = 36000   -- a finished order stays readable for ten minutes
local ORDERS_CAP = 256           -- finished orders kept past that, oldest dropped first
local WAITS_CAP = 128
local WAIT_STEP = 5              -- waits are examined on every 5th tick at most
local SCRIPTS_CAP = 256
local SCRIPT_MAX = 65536
local BP_CHUNK_MAX, BP_CHUNKS_MAX, BP_IDS_MAX = 65536, 64, 16
local GUARD_RADIUS = 24          -- a walk stops when an enemy comes this close
local GUARD_HP = 0.5             -- ...or the seat's health falls under this share
local WORM_SCAN = 56             -- worms are looked for this far out, then judged by range
local WORM_MARGIN = 4
local THROW_TRIES = 120          -- ticks a throw may go unaccepted before the order fails

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

local function is_point(t)
  return type(t) == "table" and type(t.x) == "number" and type(t.y) == "number"
end

local function init_state()
  storage.seats = storage.seats or {}
  storage.events = storage.events or {}
  storage.orders = storage.orders or {}
  storage.order_seq = storage.order_seq or 0
  storage.waits = storage.waits or {}
  storage.scripts = storage.scripts or {}
  storage.script_data = storage.script_data or {}
  storage.bot = nil                     -- the pre-seat singleton; gone for good
  -- A save written by an older version can hold a move order whose target is
  -- not a number pair; on_tick would crash on it. Drop such orders on load.
  for _, s in pairs(storage.seats) do
    if s.target ~= nil and not is_point(s.target) then
      s.target, s.waypoints, s.wp_i, s.path_req = nil, nil, nil, nil
    end
  end
end

script.on_init(init_state)
script.on_configuration_changed(init_state)

local function push_event(kind, data)
  local ev = storage.events
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

-- ---- orders ----------------------------------------------------------------
--
-- A walk, wait or throw outlives the RCON command that started it, so it gets
-- an order record the caller polls by id. Records hold plain data only (plus
-- an `entity` that order_status never returns): the caller reads them as JSON.

local function new_order(kind, seat, fields)
  storage.order_seq = storage.order_seq + 1
  local o = { id = storage.order_seq, kind = kind, seat = seat,
              status = "running", started = game.tick }
  for k, v in pairs(fields or {}) do o[k] = v end
  storage.orders[o.id] = o
  return o
end

local function finish_order(id, status, result)
  local o = id and storage.orders[id]
  if not (o and o.status == "running") then return end
  o.status = status
  o.finished = game.tick
  o.result = result
  push_event("order_done", { id = o.id, kind = o.kind, seat = o.seat,
                             status = status })
end

local function order_view(o)
  return { id = o.id, kind = o.kind, seat = o.seat, status = o.status,
           started = o.started, finished = o.finished, result = o.result,
           target = o.target, cond = o.cond, item = o.item, want = o.want }
end

local function prune_orders()
  local done = {}
  for id, o in pairs(storage.orders) do
    if o.status ~= "running" then
      if game.tick - o.finished > ORDER_KEEP_TICKS then
        storage.orders[id] = nil
      else
        done[#done + 1] = id
      end
    end
  end
  table.sort(done)
  for i = 1, #done - ORDERS_CAP do storage.orders[done[i]] = nil end
end

-- ---- the seat registry -----------------------------------------------------

-- Seat state for a player index, created on demand. Holding a row for a player
-- we have never driven costs nothing and spares every caller a nil check.
local function seat_state(index)
  storage.seats = storage.seats or {}
  local s = storage.seats[index]
  if not s then
    s = { index = index }
    storage.seats[index] = s
  end
  return s
end

-- Resolve a seat argument to a LuaPlayer. `seat` is the player NAME, which is
-- the seat id the realm service assigned before the client ever started -- so
-- this is a lookup, never a guess about which player appeared when.
local function resolve_player(args)
  args = args or {}
  local p
  if args.seat then
    p = game.players[args.seat]
    if not p then error("no such seat: " .. tostring(args.seat)) end
  elseif args.player_index then
    p = game.players[args.player_index]
    if not p then error("no player at index " .. tostring(args.player_index)) end
  else
    error("this call needs a 'seat' (the seat's player name)")
  end
  return p
end

-- Resolve to a player that can actually act: connected, alive, embodied.
local function acting_player(args)
  local p = resolve_player(args)
  if not p.connected then
    error("seat " .. p.name .. " is not connected")
  end
  if not (p.character and p.character.valid) then
    error("seat " .. p.name .. " has no character (dead or waiting to respawn)")
  end
  return p
end

-- Forget everything about a seat's current move. Its walking_state is the
-- caller's call. A walk order still running here was cut short; callers that
-- know the outcome finish the order first.
local function clear_motion(s, why)
  if s.walk then finish_order(s.walk, why or "stopped") end
  s.walk = nil
  s.guard = nil
  s.target = nil
  s.last_dir = nil
  s.waypoints = nil
  s.wp_i = nil
  s.path_req = nil
  s.path_wait = nil
  s.stuck = nil
  s.last_pos = nil
  s.repathed = nil
end

local function clear_mining(s)
  s.mining = nil
end

local function clear_throw(s, why)
  if s.throw then
    finish_order(s.throw.id, why or "stopped", { thrown = s.throw.thrown })
  end
  s.throw = nil
end

local function clear_orders(s, why)
  clear_motion(s, why)
  clear_mining(s)
  clear_throw(s, why)
end

-- Ask the engine for a route to s.target (async; answered by
-- on_script_path_request_finished). Uses the character's own footprint +
-- collision mask so the route is walkable for *this* body.
local function request_path(s, character)
  s.waypoints, s.wp_i = nil, nil
  s.path_wait = 0
  s.path_req = character.surface.request_path{
    bounding_box = character.prototype.collision_box,
    collision_mask = character.prototype.collision_mask,
    start = character.position,
    goal = s.target,
    force = character.force,
    radius = 0.5,
    entity_to_ignore = character,
    can_open_gates = true,
    pathfind_flags = { cache = false, prefer_straight_paths = true },
  }
end

local function on_path(e, index, s)
  s.path_req = nil
  s.path_wait = nil
  local p = game.players[index]
  local ch = p and p.character
  if not (ch and ch.valid and s.target) then return end
  if e.try_again_later then
    request_path(s, ch)              -- pathfinder saturated; just re-ask
    return
  end
  if e.path then
    local wps = {}
    for i, wp in ipairs(e.path) do
      wps[i] = { x = wp.position.x, y = wp.position.y }
    end
    s.waypoints = wps
    s.wp_i = 1
  elseif sqdist(ch.position, s.target) <= 8 * 8 then
    -- No route but the goal is near: fall back to the straight steer (the
    -- pathfinder refuses goals inside collision boxes that a direct
    -- approach can still get within reach of).
    s.waypoints = nil
  else
    local tx, ty = s.target.x, s.target.y
    p.walking_state = { walking = false }
    finish_order(s.walk, "blocked", { reason = "no_path" })
    clear_motion(s)
    push_event("path_blocked", { seat = p.name, x = tx, y = ty,
                                 reason = "no_path" })
  end
end

-- An error in an event handler is fatal to the whole server, so a path answer
-- that cannot be applied costs the seat its move, never the world.
script.on_event(defines.events.on_script_path_request_finished, function(e)
  for index, s in pairs(storage.seats or {}) do
    if s.path_req and e.id == s.path_req then
      local ok, err = pcall(on_path, e, index, s)
      if not ok then
        local p = game.players[index]
        if p and p.connected then p.walking_state = { walking = false } end
        clear_motion(s, "error")
        push_event("order_dropped", { seat = p and p.name, error = tostring(err) })
      end
      return
    end
  end
end)

-- ---- the walk guard --------------------------------------------------------

local WORM_RANGE = {}   -- prototype name -> attack range; a pure cache

local function worm_range(w)
  local r = WORM_RANGE[w.name]
  if not r then
    local ok, ap = pcall(function() return w.prototype.attack_parameters end)
    r = ok and ap and ap.range or 30
    WORM_RANGE[w.name] = r
  end
  return r
end

-- What should stop this walk now, or nil. Enemies are checked every tick; a
-- worm's reach depends on its range, so worms are judged every 5th tick.
local function threat(s, ch)
  local g = s.guard
  if g.hp > 0 and ch.health < g.hp * ch.max_health then
    return { reason = "hp", health = ch.health, max_health = ch.max_health }
  end
  local pos = ch.position
  if g.radius > 0 then
    local e = ch.surface.find_nearest_enemy{ position = pos, max_distance = g.radius,
                                             force = ch.force }
    if e and e.valid then
      return { reason = "enemy", enemy = e.name, x = e.position.x, y = e.position.y,
               distance = math.sqrt(sqdist(e.position, pos)) }
    end
  end
  if g.worms and game.tick % 5 == 0 then
    for _, w in pairs(ch.surface.find_entities_filtered{
          type = "turret", force = "enemy", position = pos, radius = WORM_SCAN }) do
      local reach = worm_range(w) + WORM_MARGIN
      if sqdist(w.position, pos) <= reach * reach then
        return { reason = "worm", enemy = w.name, x = w.position.x, y = w.position.y,
                 distance = math.sqrt(sqdist(w.position, pos)) }
      end
    end
  end
end

-- ---- the tick driver -------------------------------------------------------

local function main_inventory_count(character, item)
  local inv = character.get_main_inventory()
  return inv and inv.get_item_count(item) or 0
end

-- One tick of a seat's mining order. Re-asserts mining_state (the engine drops
-- it otherwise) and finishes when the target is gone or the order is filled.
local function drive_mining(p, s)
  local m = s.mining
  local ch = p.character
  if not (m.entity and m.entity.valid) then
    local got = m.product and (main_inventory_count(ch, m.product) - m.base) or 0
    p.mining_state = { mining = false }
    clear_mining(s)
    push_event("mined", { seat = p.name, item = m.product, count = got,
                          exhausted = true })
    return
  end
  local got = m.product and (main_inventory_count(ch, m.product) - m.base) or 0
  if got > 0 then m.idle = 0 else m.idle = (m.idle or 0) + 1 end
  if (m.want and got >= m.want) or (m.idle or 0) > MINE_TIMEOUT_TICKS then
    p.mining_state = { mining = false }
    clear_mining(s)
    push_event("mined", { seat = p.name, item = m.product, count = got,
                          exhausted = false })
    return
  end
  -- BOTH of these are per-tick, and mining_state alone is not enough: the
  -- engine mines what the player has SELECTED, so a mining_state whose target
  -- is not also the selection is accepted, reads back true, and mines nothing.
  p.update_selected_entity(m.position)
  p.mining_state = { mining = true, position = m.position }
end

-- One tick of a seat's move order: follow the waypoint chain (or straight-steer
-- as fallback), stop on arrival. No entity scans -- observe does those on demand.
local function drive_motion(p, s)
  local ch = p.character
  local pos = ch.position

  if s.guard then
    local t = threat(s, ch)
    if t then
      p.walking_state = { walking = false }
      t.x0, t.y0 = pos.x, pos.y
      finish_order(s.walk, "threat", t)
      clear_motion(s)
      t.seat = p.name
      push_event("threat", t)
      return
    end
  end

  -- Waiting on the pathfinder: hold position. Re-issue if the answer never
  -- comes -- request ids don't survive save/load or an engine re-exec.
  if s.path_req then
    s.path_wait = (s.path_wait or 0) + 1
    if s.path_wait > PATH_RETRY_TICKS then request_path(s, ch) end
    return
  end

  if sqdist(pos, s.target) <= ARRIVE_DIST * ARRIVE_DIST then
    p.walking_state = { walking = false }
    finish_order(s.walk, "arrived", { x = pos.x, y = pos.y })
    clear_motion(s)
    push_event("arrived", { seat = p.name, x = pos.x, y = pos.y })
    return
  end

  -- Stuck detection: no movement while trying to walk -> re-path once; if that
  -- also wedges, close-enough counts as arrival, otherwise give up.
  if s.last_pos and sqdist(pos, s.last_pos) < 0.0004 then
    s.stuck = (s.stuck or 0) + 1
  else
    s.stuck = 0
    s.last_pos = { x = pos.x, y = pos.y }
  end
  if (s.stuck or 0) >= STUCK_TICKS then
    if sqdist(pos, s.target) <= 1.5 * 1.5 then
      p.walking_state = { walking = false }
      finish_order(s.walk, "arrived", { x = pos.x, y = pos.y, inexact = true })
      clear_motion(s)
      push_event("arrived", { seat = p.name, x = pos.x, y = pos.y, inexact = true })
    elseif not s.repathed then
      s.repathed = true
      s.stuck = 0
      request_path(s, ch)
    else
      local tx, ty = s.target.x, s.target.y
      p.walking_state = { walking = false }
      finish_order(s.walk, "blocked", { reason = "stuck", x = pos.x, y = pos.y })
      clear_motion(s)
      push_event("path_blocked", { seat = p.name, x = tx, y = ty, reason = "stuck" })
    end
    return
  end

  -- Pick the current goal: the next waypoint, or the target itself on the final
  -- leg (the last waypoint only approximates the goal by `radius`).
  local goal = s.target
  if s.waypoints and s.wp_i then
    while s.wp_i <= #s.waypoints
          and sqdist(pos, s.waypoints[s.wp_i]) <= WP_ARRIVE * WP_ARRIVE do
      s.wp_i = s.wp_i + 1
      s.repathed = nil               -- progress made; allow a future re-path
    end
    if s.wp_i <= #s.waypoints then goal = s.waypoints[s.wp_i] end
  end

  -- Re-issued EVERY tick, not only on a heading change: a real player's
  -- walking_state is an input frame the engine consumes and discards.
  s.last_dir = heading(pos, goal)
  p.walking_state = { walking = true, direction = s.last_dir }
end

-- One tick of a seat's throw order: one capsule from its own inventory per
-- capsule cooldown, through the cursor the way a player throws one.
local function drive_throw(p, s)
  local t = s.throw
  if game.tick < t.next then return end
  local inv = p.character.get_main_inventory()
  local have = inv and inv.get_item_count(t.item) or 0
  if have == 0 then
    clear_throw(s, t.thrown > 0 and "out_of_items" or "no_items")
    return
  end
  local pos = t.x and { x = t.x, y = t.y } or p.character.position
  p.clear_cursor()
  local stack = inv.find_item_stack(t.item)
  if stack then p.cursor_stack.transfer_stack(stack) end
  p.use_from_cursor(pos)
  p.clear_cursor()
  local used = have - inv.get_item_count(t.item)
  if used > 0 then
    t.thrown = t.thrown + used
    t.tries = 0
    t.next = game.tick + t.cooldown
    if t.thrown >= t.want then clear_throw(s, "done") end
  else
    t.tries = t.tries + 1
    t.next = game.tick + 1
    if t.tries > THROW_TRIES then clear_throw(s, "refused") end
  end
end

-- ---- wait conditions -------------------------------------------------------

local STATUS_NAME = {}   -- defines.entity_status value -> name; a pure cache
for k, v in pairs(defines.entity_status) do STATUS_NAME[v] = k end

-- Each check returns (met, value). A target entity that is gone returns nil.
local WAIT_CHECKS = {
  ghosts = function(o)
    local surf = game.surfaces[o.surface]
    if not surf then return nil end
    local n = surf.count_entities_filtered{ area = o.area,
                                            type = { "entity-ghost", "tile-ghost" } }
    return n <= o.max, n
  end,
  items = function(o)
    local e = o.entity
    if not (e and e.valid) then return nil end
    local n = e.get_item_count(o.item)
    if o.at_least then return n >= o.at_least, n end
    return n <= o.at_most, n
  end,
  status = function(o)
    local e = o.entity
    if not (e and e.valid) then return nil end
    local name = e.status and STATUS_NAME[e.status] or "none"
    local hit = false
    for _, want in pairs(o.statuses) do
      if want == name then hit = true break end
    end
    if o.negate then hit = not hit end
    return hit, name
  end,
  products = function(o)
    local e = o.entity
    if not (e and e.valid) then return nil end
    local made = e.products_finished - o.base
    return made >= o.delta, made
  end,
  order = function(o)
    local other = storage.orders[o.order]
    local st = other and other.status or "forgotten"
    return st ~= "running", st
  end,
  ticks = function(o)
    return game.tick >= o.started + o.ticks, game.tick - o.started
  end,
}

local function check_waits()
  for id in pairs(storage.waits) do
    local o = storage.orders[id]
    if not (o and o.status == "running") then
      storage.waits[id] = nil
    elseif game.tick >= o.next then
      o.next = game.tick + o.every
      local ok, met, value = pcall(WAIT_CHECKS[o.cond], o)
      o.value = ok and value or nil
      if not ok then
        finish_order(id, "error", { error = tostring(met) })
      elseif met == nil then
        finish_order(id, "gone", {})
      elseif met then
        finish_order(id, "met", { value = value })
      elseif game.tick >= o.deadline then
        finish_order(id, "timeout", { value = value })
      end
      if o.status ~= "running" then
        o.entity = nil
        storage.waits[id] = nil
      end
    end
  end
end

script.on_event(defines.events.on_tick, function()
  for index, s in pairs(storage.seats or {}) do
    if s.target or s.mining or s.throw then
      local p = game.players[index]
      if not (p and p.connected and p.character and p.character.valid) then
        -- The seat left or died mid-order; drop the order rather than steer a
        -- ghost. Its client is stateless, so re-joining starts clean.
        clear_orders(s, "lost")
      else
        -- An error in on_tick is fatal to the whole server, so a seat whose
        -- order cannot be driven loses the order instead.
        local ok, err = pcall(function()
          if s.mining then drive_mining(p, s) end
          if s.target then drive_motion(p, s) end
          if s.throw then drive_throw(p, s) end
        end)
        if not ok then
          p.walking_state = { walking = false }
          clear_orders(s, "error")
          push_event("order_dropped", { seat = p.name, error = tostring(err) })
        end
      end
    end
  end
  if game.tick % WAIT_STEP == 0 and next(storage.waits) then
    local ok, err = pcall(check_waits)
    if not ok then
      for id in pairs(storage.waits) do finish_order(id, "error", { error = tostring(err) }) end
      storage.waits = {}
    end
  end
  if game.tick % 600 == 0 then pcall(prune_orders) end
end)

-- ---- seat lifecycle events -------------------------------------------------

script.on_event(defines.events.on_player_joined_game, function(e)
  seat_state(e.player_index)
  local p = game.players[e.player_index]
  push_event("seat_joined", { seat = p and p.name, index = e.player_index })
end)

script.on_event(defines.events.on_player_left_game, function(e)
  local s = storage.seats and storage.seats[e.player_index]
  if s then clear_orders(s, "left") end
  local p = game.players[e.player_index]
  push_event("seat_left", { seat = p and p.name, index = e.player_index })
end)

script.on_event(defines.events.on_player_died, function(e)
  local s = storage.seats and storage.seats[e.player_index]
  if s then clear_orders(s, "died") end
  local p = game.players[e.player_index]
  push_event("died", {
    seat = p and p.name,
    index = e.player_index,
    cause = e.cause and e.cause.valid and e.cause.name or nil,
  })
end)

-- ---- shared helpers --------------------------------------------------------

-- inventory contents, normalized to a {name = count} map across the 2.0
-- get_contents() array format (and the legacy map, defensively).
local function main_inventory(character)
  local inv = character.get_main_inventory()
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

-- Interactions are reach-gated so an agent must actually walk to things (no
-- acting across the map). The engine enforces its own reach on real player
-- input too; erroring here just says so in words instead of silently no-oping.
local function check_reach(character, pos)
  local reach = (character.reach_distance or 8) + 0.5
  if sqdist(character.position, pos) > reach * reach then
    error(string.format(
      "out of reach: (%.1f,%.1f) is %.1f tiles away (reach %.1f) -- move closer",
      pos.x, pos.y, math.sqrt(sqdist(character.position, pos)), reach))
  end
end

-- The closest interactable entity within ~1.5 tiles of a point (machines,
-- chests, ... -- never a character or raw resource).
local function find_target(character, pos, name)
  local best, bestd
  for _, ent in pairs(character.surface.find_entities_filtered{
        position = pos, radius = 1.5, name = name }) do
    if ent.valid and ent ~= character
       and ent.type ~= "character" and ent.type ~= "resource" then
      local d = sqdist(ent.position, pos)
      if not bestd or d < bestd then best, bestd = ent, d end
    end
  end
  return best
end

local function seat_summary(p)
  local ch = p.character
  return {
    seat = p.name,
    index = p.index,
    connected = p.connected,
    character = ch ~= nil and ch.valid,
    position = ch and ch.valid and ch.position or nil,
    surface = ch and ch.valid and ch.surface.name or nil,
    force = p.force.name,
  }
end

-- Every remote-interface argument is checked here, once, before any function
-- sees it. RCON callers send JSON, and a loosely formatted number arrives as a
-- string (bc prints 0.5 as ".5"), so numbers are coerced, which also keeps any
-- string out of `storage` and from there the tick driver.
local NUM_BOUNDS = {
  x = { -1e6, 1e6 }, y = { -1e6, 1e6 }, x1 = { -1e6, 1e6 }, y1 = { -1e6, 1e6 },
  x2 = { -1e6, 1e6 }, y2 = { -1e6, 1e6 },
  radius = { 0, 1024 }, count = { 0, 1e6 }, limit = { 1, 1000 },
  width = { 16, 4096 }, height = { 16, 4096 }, zoom = { 0.03125, 32 },
  daytime = { 0, 1 }, player_index = { 1, 65535 },
  every = { 1, 216000 }, timeout = { 1, 216000 }, ticks = { 1, 216000 },
  max = { 0, 1e9 }, at_least = { 0, 1e9 }, at_most = { 0, 1e9 }, delta = { 1, 1e9 },
  order = { 1, 2 ^ 53 }, guard_radius = { 0, 64 }, guard_hp = { 0, 1 },
}
local STR_MAX = {
  seat = 64, name = 128, recipe = 128, surface = 64, target = 128, search = 128,
  id = 64, file = 128, label = 256, direction = 32, item = 128, cond = 16,
  data = BP_CHUNK_MAX, blueprint = 4 * 1024 * 1024, code = SCRIPT_MAX,
}

local function check_args(args)
  if args == nil then return {} end
  if type(args) ~= "table" then
    error("arguments must be a table, got " .. type(args))
  end
  for k, b in pairs(NUM_BOUNDS) do
    local v = args[k]
    if v ~= nil then
      local n = type(v) == "number" and v or (type(v) == "string" and tonumber(v)) or nil
      if n == nil or n ~= n then
        error(string.format("argument %s must be a number, got %q", k, tostring(v)))
      end
      if n < b[1] or n > b[2] then
        error(string.format("argument %s must be within [%g, %g], got %g", k, b[1], b[2], n))
      end
      args[k] = n
    end
  end
  for k, max in pairs(STR_MAX) do
    local v = args[k]
    if type(v) == "number" then v = tostring(v) args[k] = v end
    if v ~= nil then
      if type(v) ~= "string" then
        error(string.format("argument %s must be a string, got %s", k, type(v)))
      end
      if #v > max then
        error(string.format("argument %s is %d characters, over %d", k, #v, max))
      end
    end
  end
  if args.file and (not args.file:match("^[%w%-_%./]+$") or args.file:find("..", 1, true)) then
    error("argument file must be a plain relative path (letters, digits, - _ . /)")
  end
  return args
end

-- The nearest machine, chest or other placed entity within 1.5 tiles of x,y.
local function entity_at(args)
  local surf = game.surfaces[args.surface or DEFAULT_SURFACE]
  if not surf then error("no such surface: " .. tostring(args.surface)) end
  local pos = { x = args.x, y = args.y }
  local best, bestd
  for _, e in pairs(surf.find_entities_filtered{ position = pos, radius = 1.5,
                                                  name = args.name }) do
    if e.valid and e.type ~= "character" and e.type ~= "resource" then
      local d = sqdist(e.position, pos)
      if not bestd or d < bestd then best, bestd = e, d end
    end
  end
  if not best then error(string.format("no entity at (%.1f,%.1f)", pos.x, pos.y)) end
  return best
end

-- ---- the stored-script registry --------------------------------------------
--
-- A command's text is replicated to every peer before it runs, so a long
-- script pays for its length on every call. A stored script crosses the wire
-- once and then runs by name. Its text lives in `storage`, so every peer holds
-- the same copy; COMPILED is only a cache each peer rebuilds on demand.

local COMPILED = {}

-- A script must not register event handlers: a handler added outside
-- control.lua is not in the save, so a peer that joins later runs without it
-- and desyncs.
local SCRIPT_PROXY = setmetatable({}, { __index = function(_, k)
  if k == "on_event" or k == "on_nth_tick" or k == "on_init" or k == "on_load"
     or k == "on_configuration_changed" then
    error("a stored script may not register event handlers", 2)
  end
  return script[k]
end })

local function compiled(name)
  local rec = storage.scripts[name]
  if not rec then error("unknown script: " .. tostring(name) .. "; define it with script_define") end
  local c = COMPILED[name]
  if not (c and c.code == rec.code) then
    -- One line of prefix keeps the script's own line numbers in errors.
    local fn, err = load("local _ENV=(...) return (function(...) " .. rec.code ..
                         "\nend)(select(2,...))", "=" .. name, "t")
    if not fn then error("script " .. name .. " does not compile: " .. tostring(err)) end
    c = { code = rec.code, fn = fn }
    COMPILED[name] = c
  end
  return c.fn
end

local api = {

  -- ---- registry ----

  -- Every player in the world, seat or human. The realm service holds the
  -- authoritative seat table; this is the world's own view of it.
  seats = function()
    local out = {}
    for _, p in pairs(game.players) do
      out[#out + 1] = seat_summary(p)
    end
    return { seats = out }
  end,

  -- Make sure a seat is embodied, optionally putting it somewhere specific.
  -- The engine mints a character on join, so this is a repair-and-place call,
  -- not the old "create the puppet" one. The teleport is a deliberate cheat:
  -- precise placement is what the agent is allowed to be better than a human at.
  spawn = function(args)
    args = args or {}
    local p = resolve_player(args)
    if not p.connected then error("seat " .. p.name .. " is not connected") end
    local created = false
    -- Freeplay runs an intro cutscene for a newly created player, and a player
    -- watching it has NO character for ~20s. A seat is not here for the
    -- cinematic, and this is the call that guarantees it can act.
    if p.controller_type == defines.controllers.cutscene then
      p.exit_cutscene()
    end
    if not (p.character and p.character.valid) then
      p.ticks_to_respawn = nil                 -- skip the respawn countdown
      if not (p.character and p.character.valid) then
        p.create_character()
        created = true
      end
    end
    if not (p.character and p.character.valid) then
      error("could not give seat " .. p.name .. " a character")
    end
    if args.x ~= nil and args.y ~= nil then
      local surf = args.surface and game.surfaces[args.surface] or p.character.surface
      p.teleport({ x = args.x, y = args.y }, surf)
      push_event("placed", { seat = p.name, x = args.x, y = args.y,
                             surface = surf.name, cheated = true })
    end
    local s = seat_state(p.index)
    clear_orders(s)
    return { ok = true, created = created, position = p.character.position,
             surface = p.character.surface.name, index = p.index }
  end,

  -- ---- movement ----

  set_target = function(args)
    local p = acting_player(args)
    if args.x == nil or args.y == nil then error("set_target requires x and y") end
    local s = seat_state(p.index)
    clear_motion(s)
    s.target = { x = args.x, y = args.y }
    request_path(s, p.character)
    return { ok = true, seat = p.name, target = s.target, pathfinding = true }
  end,

  stop = function(args)
    local p = resolve_player(args)
    local s = seat_state(p.index)
    clear_orders(s)
    if p.connected then
      p.walking_state = { walking = false }
      p.mining_state = { mining = false }
    end
    return { ok = true, seat = p.name }
  end,

  -- Teleport a seat. Separate from `spawn` and honest about what it is: the
  -- precision escape hatch, never how an agent is meant to travel.
  teleport = function(args)
    local p = acting_player(args)
    if args.x == nil or args.y == nil then error("teleport requires x and y") end
    local surf = args.surface and game.surfaces[args.surface] or p.character.surface
    if not surf then error("no such surface: " .. tostring(args.surface)) end
    local s = seat_state(p.index)
    clear_orders(s)
    if not p.teleport({ x = args.x, y = args.y }, surf) then
      error(string.format("cannot teleport to (%.1f,%.1f)", args.x, args.y))
    end
    push_event("placed", { seat = p.name, x = args.x, y = args.y,
                           surface = surf.name, cheated = true })
    return { ok = true, seat = p.name, position = p.character.position,
             surface = surf.name, cheated = true }
  end,

  -- ---- gameplay ----

  -- Real mining: hand the engine a mining_state and let it run. The tick driver
  -- re-asserts it and reports what actually landed in the seat's inventory. No
  -- script decrements the resource -- the engine does, at the game's own rate,
  -- which is why the yield counts as the player's.
  mine = function(args)
    args = args or {}
    local p = acting_player(args)
    if args.x == nil or args.y == nil then error("mine requires x and y") end
    local ch = p.character
    local pos = { x = args.x, y = args.y }
    check_reach(ch, pos)
    local target, bestd
    for _, ent in pairs(ch.surface.find_entities_filtered{
          position = pos, radius = 1.5, name = args.name }) do
      if ent.valid and ent ~= ch and ent.type ~= "character"
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
    local product
    for _, prod in pairs(props.products or {}) do
      if prod.type == "item" then product = prod.name break end
    end
    local s = seat_state(p.index)
    clear_mining(s)
    s.mining = {
      position = { x = target.position.x, y = target.position.y },
      entity = target,
      product = product,
      base = product and main_inventory_count(ch, product) or 0,
      want = args.count,
      idle = 0,
    }
    return { ok = true, seat = p.name, mining = target.name, product = product,
             want = args.count, position = s.mining.position }
  end,

  -- The real crafting queue. Unlike a scripted insert, this is counted by
  -- production statistics -- which is what makes craft-item research triggers
  -- fire on their own.
  craft = function(args)
    args = args or {}
    local p = acting_player(args)
    if not args.recipe then error("craft requires 'recipe'") end
    local rec = p.force.recipes[args.recipe]
    if not rec then error("unknown recipe: " .. args.recipe) end
    if not rec.enabled then error("recipe not unlocked: " .. args.recipe) end
    local queued = p.begin_crafting{ recipe = args.recipe, count = args.count or 1 }
    if queued == 0 then
      error("cannot craft " .. args.recipe .. " (missing ingredients?)")
    end
    return { ok = true, seat = p.name, queued = queued }
  end,

  build = function(args)
    args = args or {}
    local p = acting_player(args)
    local ch = p.character
    if not args.name then error("build requires 'name' (the item to place)") end
    if args.x == nil or args.y == nil then error("build requires x and y") end
    local pos = { x = args.x, y = args.y }
    check_reach(ch, pos)
    local inv = ch.get_main_inventory()
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
    if not ch.surface.can_place_entity{
          name = ename, position = pos, direction = dir, force = ch.force,
          build_check_type = defines.build_check_type.manual } then
      error(string.format("cannot place %s at (%.1f,%.1f) -- collision or invalid",
                          ename, pos.x, pos.y))
    end
    local ent = ch.surface.create_entity{
      name = ename, position = pos, direction = dir, force = ch.force,
      player = p, raise_built = true }
    if not ent then error("placement failed") end
    inv.remove{ name = args.name, count = 1 }   -- only after success
    return { ok = true, seat = p.name, built = ename, position = ent.position }
  end,

  insert = function(args)
    args = args or {}
    local p = acting_player(args)
    local ch = p.character
    if not args.name then error("insert requires 'name'") end
    if args.x == nil or args.y == nil then
      error("insert requires x and y (the target entity)")
    end
    local pos = { x = args.x, y = args.y }
    check_reach(ch, pos)
    local target = find_target(ch, pos, args.target)
    if not target then
      error(string.format("no entity at (%.1f,%.1f)", pos.x, pos.y))
    end
    local inv = ch.get_main_inventory()
    local have = inv and inv.get_item_count(args.name) or 0
    if have < 1 then error("no " .. args.name .. " in inventory") end
    -- entity.insert routes to the right slot (furnace fuel vs input, etc.)
    local put = target.insert{ name = args.name,
                               count = math.min(args.count or have, have) }
    if put == 0 then error(target.name .. " did not accept " .. args.name) end
    inv.remove{ name = args.name, count = put }
    return { ok = true, seat = p.name, inserted = put, target = target.name }
  end,

  take = function(args)
    args = args or {}
    local p = acting_player(args)
    local ch = p.character
    if not args.name then error("take requires 'name'") end
    if args.x == nil or args.y == nil then
      error("take requires x and y (the source entity)")
    end
    local pos = { x = args.x, y = args.y }
    check_reach(ch, pos)
    local target = find_target(ch, pos, args.target)
    if not target then
      error(string.format("no entity at (%.1f,%.1f)", pos.x, pos.y))
    end
    local inv = ch.get_main_inventory()
    if not inv then error("seat has no inventory") end
    local avail = target.get_item_count(args.name)
    if avail < 1 then error(target.name .. " has no " .. args.name) end
    local got = target.remove_item{ name = args.name,
                                    count = math.min(args.count or avail, avail) }
    local kept = inv.insert{ name = args.name, count = got }
    if kept < got then
      target.insert{ name = args.name, count = got - kept }  -- overflow back
    end
    if kept == 0 then error("inventory full") end
    return { ok = true, seat = p.name, taken = kept, from = target.name }
  end,

  -- Force-wide, and now genuinely a cheat rather than the only path: seats
  -- craft through the real queue, so craft-item trigger technologies advance on
  -- their own. One shared force means this unlocks the tech for EVERY seat and
  -- for any human in the world, which is why it stays flagged and loud.
  research = function(args)
    args = args or {}
    if not args.name then error("research requires 'name'") end
    local force = game.forces.player
    local tech = force.technologies[args.name]
    if not tech then error("unknown technology: " .. args.name) end
    if tech.researched then
      return { ok = true, researched = args.name, already = true, cheated = false }
    end
    tech.researched = true
    push_event("researched", { name = args.name, cheated = true })
    return { ok = true, researched = args.name, cheated = true }
  end,

  -- ---- catalog queries (bounded: filter + cap, the RCON channel fragments
  -- past 4 KB) ----

  -- `craftable` is the whole point of passing a seat here: a force recipe list
  -- includes smelting and assembler-only recipes, and begin_crafting refuses
  -- those. Asking the player itself is the only answer that accounts for both
  -- the crafting category and what is in its hands right now.
  recipes = function(args)
    args = args or {}
    local force = game.forces.player
    local limit = args.limit or 40
    local p = args.seat and game.players[args.seat] or nil
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
        local row = { name = name, ingredients = ing, products = prod }
        if p then row.craftable = p.get_craftable_count(name) end
        out[#out + 1] = row
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

  -- ---- blueprints ----
  --
  -- A blueprint string is how a human moves a DESIGNED thing into the world,
  -- and it is the one place an agent is meant to be more precise than a human.
  -- Both directions dodge the RCON channel's ~4 KB fragmentation: a string
  -- comes IN as chunks accumulated in storage, and goes OUT as a file the
  -- service reads off the bind-mounted script-output.

  -- Append one chunk of an inbound blueprint string. `reset` starts it over.
  bp_put = function(args)
    args = args or {}
    storage.bp = storage.bp or {}
    local id = tostring(args.id or "default")
    if args.reset then storage.bp[id] = {} end
    if not storage.bp[id] and table_size(storage.bp) >= BP_IDS_MAX then
      error("too many blueprint uploads in progress (" .. BP_IDS_MAX .. "); stamp or reset one")
    end
    local buf = storage.bp[id] or {}
    if #buf >= BP_CHUNKS_MAX then
      error("blueprint " .. id .. " is over " .. BP_CHUNKS_MAX .. " chunks; reset it")
    end
    buf[#buf + 1] = args.data or ""
    storage.bp[id] = buf
    return { ok = true, id = id, chunks = #buf }
  end,

  -- ALL OR NOTHING, measured 2026-09-05 on 2.1.17: if ONE entity of a
  -- blueprint cannot go down, build_blueprint places ZERO ghosts -- not the
  -- rest of the module. Verified by counting can_place_entity over the
  -- blueprint's own entities at the target: blocked=1 gives ghosts=0.
  -- force_build does not override it (that covers overlapping your own
  -- entities, not obstructed ground). This is why a bigger module looks like
  -- it hits a size limit -- more tiles, more chance of one tree in the way --
  -- and why the same blueprint places on one map seed and not the next. So we
  -- pre-count the obstructions and say where they are, and `clear` removes the
  -- trees and rocks first, which is what a player's construction bots do.
  -- Stamp an accumulated (or inline) blueprint at a position. Ghosts by
  -- default, because that is what placing a blueprint does; `build = true`
  -- revives them immediately, which pays no items and is therefore a cheat.
  blueprint_stamp = function(args)
    args = args or {}
    local p = acting_player(args)
    local id = tostring(args.id or "default")
    local text = args.blueprint
    if not text then
      local buf = (storage.bp or {})[id]
      if not buf or #buf == 0 then error("no blueprint uploaded for id " .. id) end
      text = table.concat(buf)
    end
    if args.x == nil or args.y == nil then error("stamp requires x and y") end
    local surf = args.surface and game.surfaces[args.surface] or p.character.surface
    if not surf then error("no such surface: " .. tostring(args.surface)) end
    local dir = defines.direction.north
    if args.direction then
      dir = defines.direction[args.direction]
      if dir == nil then error("bad direction: " .. tostring(args.direction)) end
    end

    local inv = game.create_inventory(1)
    local ok, result = pcall(function()
      local stack = inv[1]
      stack.set_stack{ name = "blueprint" }
      -- -1 = not importable at all; 1 = imported but something was dropped
      -- (a mod entity this save does not have), which is worth reporting.
      local status = stack.import_stack(text)
      if status == -1 then error("not a valid blueprint string") end
      if not stack.is_blueprint_setup() then
        error("blueprint string decoded to an EMPTY blueprint")
      end
      local wanted = stack.get_blueprint_entity_count()
      -- Where the module will actually land, so the caller can be told which
      -- part of it fell off the edge of the generated world.
      local corners = {}
      local ents = stack.get_blueprint_entities()
      -- Find every obstruction BEFORE building: one of them means nothing at
      -- all is placed, so an unexplained zero has to come back with a reason.
      local blocked, blocked_at, cleared = 0, {}, 0
      for _, e in pairs(ents or {}) do
        local px, py = args.x + e.position.x, args.y + e.position.y
        if not surf.can_place_entity{
              name = e.name, position = { x = px, y = py }, force = p.force,
              direction = e.direction,
              build_check_type = defines.build_check_type.blueprint_ghost } then
          if args.clear then
            for _, obstruction in pairs(surf.find_entities_filtered{
                  position = { x = px, y = py }, radius = 2,
                  type = { "tree", "simple-entity" } }) do
              if obstruction.valid then obstruction.destroy() cleared = cleared + 1 end
            end
          end
          if not surf.can_place_entity{
                name = e.name, position = { x = px, y = py }, force = p.force,
                direction = e.direction,
                build_check_type = defines.build_check_type.blueprint_ghost } then
            blocked = blocked + 1
            if #blocked_at < 5 then
              -- Say WHY. Trees and rocks `clear` can remove; a cliff or water
              -- means move the module or landfill, which is a different job.
              local why
              for _, o in pairs(surf.find_entities_filtered{
                    position = { x = px, y = py }, radius = 1.5 }) do
                if o.valid and o.type ~= "character" then why = o.name break end
              end
              -- On ungenerated map there is no tile at all, and touching the
              -- invalid LuaTile throws -- which is exactly the case we are
              -- here to report on.
              local tile = surf.get_tile(math.floor(px), math.floor(py))
              blocked_at[#blocked_at + 1] = {
                name = e.name, x = px, y = py,
                tile = (tile and tile.valid) and tile.name or nil,
                obstruction = why }
            end
          end
        end
      end
      if ents and #ents > 0 then
        local x1, y1 = math.huge, math.huge
        local x2, y2 = -math.huge, -math.huge
        for _, e in pairs(ents) do
          x1 = math.min(x1, e.position.x); x2 = math.max(x2, e.position.x)
          y1 = math.min(y1, e.position.y); y2 = math.max(y2, e.position.y)
        end
        corners = { { x = args.x + x1, y = args.y + y1 },
                    { x = args.x + x2, y = args.y + y1 },
                    { x = args.x + x1, y = args.y + y2 },
                    { x = args.x + x2, y = args.y + y2 } }
      end
      local ghosts = stack.build_blueprint{
        surface = surf, force = p.force, position = { x = args.x, y = args.y },
        direction = dir, skip_fog_of_war = false,
        force_build = args.force_build and true or false,
        raise_built = true,
      }
      local built, revived = 0, 0
      for _, g in pairs(ghosts) do
        if g and g.valid then
          built = built + 1
          if args.build then
            local _, ent = g.revive{ raise_revive = true }
            if ent and ent.valid then revived = revived + 1 end
          end
        end
      end
      return { wanted = wanted, placed = built, revived = revived,
               blocked = blocked, blocked_at = blocked_at, cleared = cleared,
               lossy = status == 1, corners = corners,
               footprint = corners[1] and
                 { x1 = corners[1].x, y1 = corners[1].y,
                   x2 = corners[4].x, y2 = corners[4].y } or nil }
    end)
    inv.destroy()
    if not ok then error(result) end
    if storage.bp then storage.bp[id] = nil end

    -- Report what did NOT land: stamping onto occupied ground fails per
    -- entity, and claiming success for a half-placed module is worse than
    -- placing nothing.
    local skipped = result.wanted - result.placed
    -- The other way to place nothing is to aim at map that does not exist yet,
    -- which looks identical to "everything collided". Check the module's whole
    -- FOOTPRINT, not just the anchor: a big blueprint reaches past the anchor
    -- chunk, and it is the far corner that is usually missing.
    local generated = true
    for _, c in pairs(result.corners or {}) do
      if not surf.is_chunk_generated{ x = math.floor(c.x / 32),
                                      y = math.floor(c.y / 32) } then
        generated = false
        break
      end
    end
    if skipped > 0 then
      push_event("blueprint_partial", { seat = p.name, placed = result.placed,
                                        skipped = skipped,
                                        ungenerated = not generated })
    end
    return { ok = true, seat = p.name, position = { x = args.x, y = args.y },
             entities = result.wanted, placed = result.placed,
             skipped = skipped, built = result.revived,
             cheated = args.build and true or false,
             lossy = result.lossy, chunk_generated = generated,
             footprint = result.footprint, blocked = result.blocked,
             blocked_at = result.blocked_at, cleared = result.cleared,
             direction = args.direction or "north" }
  end,

  -- Capture a region as a blueprint string, written to script-output.
  blueprint_capture = function(args)
    args = args or {}
    local p = resolve_player(args)
    local surf = args.surface and game.surfaces[args.surface]
                 or (p.character and p.character.valid and p.character.surface)
                 or game.surfaces[1]
    local area
    if args.radius then
      local c = p.character
      if not (c and c.valid) then error("radius needs an embodied seat") end
      local cx = args.x or c.position.x
      local cy = args.y or c.position.y
      area = { { cx - args.radius, cy - args.radius },
               { cx + args.radius, cy + args.radius } }
    elseif args.x1 and args.y1 and args.x2 and args.y2 then
      area = { { math.min(args.x1, args.x2), math.min(args.y1, args.y2) },
               { math.max(args.x1, args.x2), math.max(args.y1, args.y2) } }
    else
      error("capture needs radius, or x1/y1/x2/y2")
    end
    local file = args.file or string.format("bp-%s-%d.txt", p.name, game.tick)
    local inv = game.create_inventory(1)
    local ok, result = pcall(function()
      local stack = inv[1]
      stack.set_stack{ name = "blueprint" }
      stack.create_blueprint{
        surface = surf, force = p.force, area = area,
        always_include_tiles = args.include_tiles and true or false,
      }
      local n = stack.get_blueprint_entity_count()
      if n == 0 and not args.include_tiles then
        error("nothing to capture in that area")
      end
      -- create_blueprint leaves entity positions in ABSOLUTE world
      -- coordinates. build_blueprint then adds the stamp position to them, so
      -- a module captured at (200,212) and stamped at (600,600) lands at
      -- (800,812) -- usually on ungenerated map, where it silently places
      -- nothing. Re-centre on the bounding box, which is what the game does
      -- when a player makes a blueprint by hand, so "stamp at (x,y)" means
      -- the module's middle is at (x,y).
      local ents = stack.get_blueprint_entities()
      if ents and #ents > 0 then
        local minx, miny = math.huge, math.huge
        local maxx, maxy = -math.huge, -math.huge
        for _, e in pairs(ents) do
          minx = math.min(minx, e.position.x); maxx = math.max(maxx, e.position.x)
          miny = math.min(miny, e.position.y); maxy = math.max(maxy, e.position.y)
        end
        -- Shift by a WHOLE number of tiles. Factorio aligns a 1x1 entity on a
        -- half-tile and a 2x2 on a whole one, so a fractional shift puts every
        -- entity off its grid and the whole stamp silently places nothing.
        local cx = math.floor((minx + maxx) / 2 + 0.5)
        local cy = math.floor((miny + maxy) / 2 + 0.5)
        for _, e in pairs(ents) do
          e.position = { x = e.position.x - cx, y = e.position.y - cy }
        end
        stack.set_blueprint_entities(ents)
        local tiles = stack.get_blueprint_tiles()
        if tiles and #tiles > 0 then
          for _, t in pairs(tiles) do
            t.position = { x = t.position.x - cx, y = t.position.y - cy }
          end
          stack.set_blueprint_tiles(tiles)
        end
      end
      if args.label then stack.label = args.label end
      -- for_player 0 = write on the server, which is the peer we can read.
      helpers.write_file(file, stack.export_stack(), false, 0)
      return n
    end)
    inv.destroy()
    if not ok then error(result) end
    return { ok = true, seat = p.name, file = file, entities = result,
             area = { x1 = area[1][1], y1 = area[1][2],
                      x2 = area[2][1], y2 = area[2][2] },
             surface = surf.name }
  end,

  -- ---- vision ----

  -- A screenshot is rendered by ONE peer -- the seat named here -- and written
  -- inside that seat's own container, which is why every seat bind-mounts its
  -- script-output to a host directory the service can read. The engine returns
  -- before the file exists, so the caller waits on the file, not on this.
  --
  -- Resolution is a per-call argument rendered offscreen, so it is NOT bounded
  -- by the seat's tiny (and unmapped) window: a seat can stay dark and still
  -- see clearly -- verified identical against a seat whose window was left
  -- mapped.
  --
  -- The engine does NOT draw by_player's own character. A seat photographing
  -- itself sees bare ground where it stands; every OTHER player is drawn
  -- normally. The frame is centred on the seat, so its position is known
  -- rather than visible.
  screenshot = function(args)
    args = args or {}
    local p = resolve_player(args)
    if not p.connected then error("seat " .. p.name .. " is not connected") end
    local ch = p.character
    local pos
    if args.x ~= nil and args.y ~= nil then
      pos = { x = args.x, y = args.y }
    elseif ch and ch.valid then
      pos = { x = ch.position.x, y = ch.position.y }
    else
      error("seat " .. p.name .. " has no character; pass x and y")
    end
    local surf = args.surface and game.surfaces[args.surface]
                 or (ch and ch.valid and ch.surface) or game.surfaces[1]
    if not surf then error("no such surface: " .. tostring(args.surface)) end
    local file = args.file or
        string.format("%s-%d.png", p.name, game.tick)
    game.take_screenshot{
      by_player = p,
      surface = surf,
      position = pos,
      resolution = { x = args.width or 1280, y = args.height or 720 },
      zoom = args.zoom or 1,
      path = file,
      show_gui = false,
      show_entity_info = args.show_entity_info and true or false,
      -- Lit as noon unless the caller asks otherwise: a night shot of a factory
      -- is a black rectangle, and the point of this verb is to be looked at.
      daytime = args.daytime or 0,
      anti_alias = true,
    }
    return { ok = true, seat = p.name, file = file, position = pos,
             surface = surf.name,
             resolution = { x = args.width or 1280, y = args.height or 720 },
             zoom = args.zoom or 1 }
  end,

  -- Return-and-clear in ONE call so no event can slip between read and reset;
  -- the supervisor's poller re-emits these up the hub as rlm.factorio events.
  -- ---- orders: they outlive the command that starts them ----

  version = function()
    return { version = script.active_mods[script.mod_name] }
  end,

  -- Walk with a guard: the seat stops by itself when an enemy comes within
  -- guard_radius, a worm could reach it, or its health falls under guard_hp
  -- of max. guard = false walks blind, which is how a seat retreats.
  walk = function(args)
    local p = acting_player(args)
    if args.x == nil or args.y == nil then error("walk requires x and y") end
    local s = seat_state(p.index)
    clear_motion(s, "superseded")
    s.target = { x = args.x, y = args.y }
    if args.guard ~= false then
      s.guard = { radius = args.guard_radius or GUARD_RADIUS,
                  hp = args.guard_hp or GUARD_HP,
                  worms = args.guard_worms ~= false }
    end
    local o = new_order("walk", p.name, { target = s.target })
    s.walk = o.id
    request_path(s, p.character)
    return { ok = true, order = o.id, seat = p.name, target = s.target, guard = s.guard }
  end,

  -- Watch for a condition, checked every `every` ticks until it holds or
  -- `timeout` ticks pass. The order ends met, timeout, gone (the entity was
  -- removed) or error.
  wait = function(args)
    local cond = args.cond
    if not WAIT_CHECKS[cond or ""] then
      error("wait needs cond: ghosts, items, status, products, order or ticks")
    end
    if table_size(storage.waits) >= WAITS_CAP then
      error("too many waits running (" .. WAITS_CAP .. "); let some finish")
    end
    local o = { cond = cond, every = math.max(args.every or 30, WAIT_STEP),
                deadline = game.tick + (args.timeout or 3600), next = game.tick }
    if cond == "ghosts" then
      o.surface = args.surface or DEFAULT_SURFACE
      if not game.surfaces[o.surface] then error("no such surface: " .. o.surface) end
      if args.x1 and args.y1 and args.x2 and args.y2 then
        o.area = { { math.min(args.x1, args.x2), math.min(args.y1, args.y2) },
                   { math.max(args.x1, args.x2), math.max(args.y1, args.y2) } }
      elseif args.x and args.y and args.radius then
        o.area = { { args.x - args.radius, args.y - args.radius },
                   { args.x + args.radius, args.y + args.radius } }
      else
        error("ghosts needs x1,y1,x2,y2, or x,y,radius")
      end
      if o.area[2][1] - o.area[1][1] > 1024 or o.area[2][2] - o.area[1][2] > 1024 then
        error("ghosts area is over 1024 tiles on a side; split it")
      end
      o.max = args.max or 0
    elseif cond == "items" or cond == "status" or cond == "products" then
      if args.x ~= nil and args.y ~= nil then
        o.entity = entity_at(args)
      elseif cond == "items" and args.seat then
        o.entity = acting_player(args).character
      else
        error(cond .. " needs the entity's x,y" .. (cond == "items" and ", or a seat" or ""))
      end
      o.target = { name = o.entity.name, x = o.entity.position.x, y = o.entity.position.y }
      if cond == "items" then
        if not (args.item and prototypes.item[args.item]) then
          error("items needs 'item', an item name")
        end
        if (args.at_least == nil) == (args.at_most == nil) then
          error("items needs exactly one of at_least, at_most")
        end
        o.item, o.at_least, o.at_most = args.item, args.at_least, args.at_most
      elseif cond == "status" then
        local st = args.status
        if type(st) == "string" then st = { st } end
        if type(st) ~= "table" or #st == 0 or #st > 32 then
          error("status needs 'status': a status name or a list of them")
        end
        for _, n in pairs(st) do
          if n ~= "none" and defines.entity_status[n] == nil then
            error("unknown status: " .. tostring(n))
          end
        end
        o.statuses, o.negate = st, args.negate and true or false
      else
        local ok, base = pcall(function() return o.entity.products_finished end)
        if not ok then error(o.entity.name .. " does not count finished products") end
        o.base, o.delta = base, args.delta or 1
      end
    elseif cond == "order" then
      if not args.order then error("order needs 'order', an order id") end
      o.order = args.order
    else
      o.ticks = args.ticks or 60
    end
    local rec = new_order("wait", args.seat, o)
    storage.waits[rec.id] = true
    return { ok = true, order = rec.id }
  end,

  -- Throw `count` capsules from the seat's own inventory, one per capsule
  -- cooldown, at x,y or at the seat's feet.
  throw = function(args)
    local p = acting_player(args)
    local proto = args.item and prototypes.item[args.item]
    if not (proto and proto.type == "capsule") then
      error("throw needs 'item', a capsule (got " .. tostring(args.item) .. ")")
    end
    if (args.x == nil) ~= (args.y == nil) then error("throw takes both x and y, or neither") end
    local cooldown = 30
    pcall(function() cooldown = proto.capsule_action.attack_parameters.cooldown end)
    local s = seat_state(p.index)
    clear_throw(s, "superseded")
    local want = math.max(1, args.count or 1)
    local o = new_order("throw", p.name, { item = args.item, want = want })
    s.throw = { id = o.id, item = args.item, want = want, thrown = 0, tries = 0,
                next = game.tick, cooldown = math.max(1, math.ceil(cooldown)),
                x = args.x, y = args.y }
    return { ok = true, order = o.id, seat = p.name, cooldown = s.throw.cooldown }
  end,

  order_status = function(args)
    local ids = args.ids or { args.order }
    if type(ids) ~= "table" or #ids == 0 or #ids > 64 then
      error("order_status takes 'order', or 'ids' (1-64 order ids)")
    end
    local out = {}
    for _, id in ipairs(ids) do
      local o = storage.orders[tonumber(id) or 0]
      out[#out + 1] = o and order_view(o) or { id = id, status = "unknown" }
    end
    return { orders = out, tick = game.tick }
  end,

  -- ---- stored scripts ----

  script_define = function(args)
    local name, code = args.name, args.code
    if not (name and name:match("^[%w%-_%.:]+$") and #name <= 64) then
      error("script name must be 1-64 letters, digits, - _ . :")
    end
    if not code then error("script_define requires 'code'") end
    if not storage.scripts[name] and table_size(storage.scripts) >= SCRIPTS_CAP then
      local oldest, at
      for n, rec in pairs(storage.scripts) do
        if not at or rec.used < at then oldest, at = n, rec.used end
      end
      storage.scripts[oldest] = nil
      COMPILED[oldest] = nil
    end
    storage.scripts[name] = { code = code, defined = game.tick, used = game.tick }
    local ok, err = pcall(compiled, name)
    if not ok then
      storage.scripts[name] = nil
      COMPILED[name] = nil
      error(err, 0)
    end
    return { ok = true, name = name, size = #code }
  end,

  -- Run a stored script with `args` as its `...`. It sees the game's globals,
  -- a fresh table for its own, and `storage` bound to a table of its own.
  script_run = function(args)
    local fn = compiled(args.name)
    storage.scripts[args.name].used = game.tick
    local env = setmetatable({ storage = storage.script_data, script = SCRIPT_PROXY },
                             { __index = _G })
    return fn(env, args.args)
  end,

  script_list = function()
    local out = {}
    for n, rec in pairs(storage.scripts) do
      out[#out + 1] = { name = n, size = #rec.code, used = rec.used }
    end
    return { scripts = out }
  end,

  script_drop = function(args)
    storage.scripts[args.name or ""] = nil
    COMPILED[args.name or ""] = nil
    return { ok = true }
  end,

  drain_events = function()
    local ev = storage.events or {}
    storage.events = {}
    return { events = ev }
  end,

  observe = function(args)
    args = args or {}
    local p = resolve_player(args)
    local s = seat_state(p.index)
    local out = {
      tick = game.tick,
      paused = game.tick_paused,
      seat = p.name,
      index = p.index,
      connected = p.connected,
      force = p.force.name,
      body = false,
    }

    -- Who else is in the world. On one shared force everything below is
    -- everyone's, so an agent needs to know who it is sharing with.
    local others = {}
    for _, q in pairs(game.players) do
      if q.index ~= p.index then others[#others + 1] = seat_summary(q) end
    end
    out.players = others

    local ch = p.character
    if ch and ch.valid then
      local ws = p.walking_state
      out.body = true
      out.surface = ch.surface.name
      out.position = ch.position
      out.health = ch.health
      out.max_health = ch.max_health
      out.walking = ws and ws.walking or false
      out.target = s.target
      out.mining = s.mining and { x = s.mining.position.x, y = s.mining.position.y,
                                  item = s.mining.product } or nil
      out.inventory = main_inventory(ch)
      out.reach = ch.reach_distance
      local q = {}
      for _, item in pairs(p.crafting_queue or {}) do
        q[#q + 1] = { recipe = item.recipe, count = item.count }
      end
      out.crafting = q
      local radius = math.min(args.radius or DEFAULT_RADIUS, 128)
      local near = {}
      for _, ent in pairs(ch.surface.find_entities_filtered{
            position = ch.position, radius = radius, limit = NEARBY_CAP + 1 }) do
        if ent ~= ch and ent.valid then
          local rec = { name = ent.name, type = ent.type,
                        x = ent.position.x, y = ent.position.y }
          if ent.type == "resource" then rec.amount = ent.amount end
          -- Name the people. Without this a teammate reads as an anonymous
          -- "character" entity indistinguishable from scenery.
          if ent.type == "character" and ent.player then
            rec.seat = ent.player.name
          end
          near[#near + 1] = rec
          if #near >= NEARBY_CAP then break end
        end
      end
      out.nearby = near
    end
    return out
  end,
}

local wrapped = {}
for name, fn in pairs(api) do
  wrapped[name] = function(args, ...) return fn(check_args(args), ...) end
end
remote.add_interface("game_bot", wrapped)

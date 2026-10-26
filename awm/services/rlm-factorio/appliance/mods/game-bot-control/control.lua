-- game-bot-control: per-seat control of server-held drone bodies.
--
-- A seat is a `game-bot-drone` character (data-final-fixes.lua) that no
-- client owns: the server holds it, so a seat costs one entity instead of a
-- client process. A character still plays by the game's rules -- it walks at
-- running speed, mines at its mining speed, hand-crafts through the real
-- queue and dies to biters -- and three things a player gets for free are
-- done here instead:
--
--  * CRAFTING STATISTICS. A character with no player adds nothing to
--    production statistics and fires no craft-item trigger. The tick driver
--    diffs each drone's crafting queue and books every finished craft with
--    on_flow, which is also why on_flow never reaches agent code.
--  * RESPAWN. A dead drone leaves a corpse holding its inventory, and a new
--    one appears at the force's spawn after RESPAWN_TICKS.
--  * CHARTING. The force charts around each drone every CHART_TICKS.
--
-- The drone collides with nothing, so a walk is a straight line and needs no
-- pathfinder. A move faster than running speed raises `speed_violation`: only
-- a teleport can do that, and only admin code may teleport.
--
-- All mutable state lives in `storage`, which serializes into the save, so
-- seats survive save/load. Seats are keyed by seat name.

local DRONE = "game-bot-drone"
local DEFAULT_SURFACE = "nauvis"
local ARRIVE_DIST = 0.2          -- tiles; within this we consider the seat arrived
local STUCK_TICKS = 60           -- ticks without progress before a walk gives up
local RESPAWN_TICKS = 600
local CHART_TICKS, CHART_RADIUS = 300, 128
local CRAFT_TICKS = 30           -- how often finished crafts are booked
local SPEED_SLACK = 1.25         -- a step this many times running speed is a violation
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
local TOOLS_CAP = 256
local BP_CHUNK_MAX, BP_CHUNKS_MAX, BP_IDS_MAX = 65536, 64, 16
local GUARD_RADIUS = 24          -- a walk stops when an enemy comes this close
local GUARD_HP = 0.5             -- ...or the seat's health falls under this share
local WORM_SCAN = 56             -- worms are looked for this far out, then judged by range
local WORM_MARGIN = 4

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
  storage.tools = storage.tools or {}
  storage.tool_data = storage.tool_data or {}
  storage.bot = nil                     -- the pre-seat singleton; gone for good
  -- Seats were once keyed by player index and held no body. Such rows, and a
  -- move order whose target is not a number pair, would crash on_tick.
  for key, s in pairs(storage.seats) do
    if type(key) ~= "string" or s.name ~= key then
      storage.seats[key] = nil
    elseif s.target ~= nil and not is_point(s.target) then
      s.target = nil
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

-- `seat` is the name the realm service assigned at join.
local function resolve_seat(args)
  if not (args and args.seat) then error("this call needs a 'seat' (the seat's name)") end
  local s = storage.seats[args.seat]
  if not s then error("no such seat: " .. tostring(args.seat) .. "; spawn it first") end
  return s
end

-- Where build_blueprint puts each entity of a stamp at (x, y) turned by dir. A 2.x blueprint keeps its entities'
-- world positions: the engine turns them about the origin, then centres the turned set on the floor of its tile
-- box's midpoint. Checked against build_blueprint in all four directions.
local function stamp_plan(ents, x, y, dir)
  local turned = {}
  for _, e in pairs(ents) do
    local ex, ey = e.position.x, e.position.y
    if dir == 4 then ex, ey = -ey, ex elseif dir == 8 then ex, ey = -ex, -ey elseif dir == 12 then ex, ey = ey, -ex end
    turned[#turned + 1] = { name = e.name, x = ex, y = ey, direction = ((e.direction or 0) + dir) % 16 }
  end
  local x1, y1, x2, y2 = math.huge, math.huge, -math.huge, -math.huge
  for _, t in pairs(turned) do
    local proto = prototypes.entity[t.name]
    local w, h = proto and proto.tile_width or 1, proto and proto.tile_height or 1
    if t.direction == 4 or t.direction == 12 then w, h = h, w end
    t.w, t.h = w, h
    x1, y1 = math.min(x1, t.x - w / 2), math.min(y1, t.y - h / 2)
    x2, y2 = math.max(x2, t.x + w / 2), math.max(y2, t.y + h / 2)
  end
  local cx, cy = math.floor((x1 + x2) / 2), math.floor((y1 + y2) / 2)
  for _, t in pairs(turned) do t.x, t.y = x + t.x - cx, y + t.y - cy end
  return turned
end

-- A seat that can act right now, and its drone.
local function acting_seat(args)
  local s = resolve_seat(args)
  local d = s.drone
  if not (d and d.valid) then
    error("seat " .. s.name .. " has no drone (dead, respawn at tick " .. tostring(s.respawn_at) .. ")")
  end
  return s, d
end

-- Forget everything about a seat's current move. Its walking_state is the
-- caller's call. A walk order still running here was cut short; callers that
-- know the outcome finish the order first.
local function clear_motion(s, why)
  if s.walk then finish_order(s.walk, why or "stopped") end
  s.walk = nil
  s.guard = nil
  s.target = nil
  s.stuck = nil
  s.last_pos = nil
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

local function halt(d)
  d.walking_state = { walking = false, direction = d.walking_state.direction }
end

-- One tick of a seat's mining order. Re-asserts mining_state and the selection
-- (the engine mines what is SELECTED) and finishes when the target is gone or
-- the order is filled.
local function drive_mining(d, s)
  local m = s.mining
  local got = m.product and (main_inventory_count(d, m.product) - m.base) or 0
  if not (m.entity and m.entity.valid) then
    d.mining_state = { mining = false }
    clear_mining(s)
    push_event("mined", { seat = s.name, item = m.product, count = got, exhausted = true })
    return
  end
  if got > (m.got or 0) then m.idle = 0 else m.idle = (m.idle or 0) + 1 end
  m.got = got
  if (m.want and got >= m.want) or m.idle > MINE_TIMEOUT_TICKS then
    d.mining_state = { mining = false }
    clear_mining(s)
    push_event("mined", { seat = s.name, item = m.product, count = got, exhausted = false })
    return
  end
  d.update_selected_entity(m.position)
  d.mining_state = { mining = true, position = m.position }
end

-- One tick of a seat's move order: steer straight at the target, stop on
-- arrival, a threat, or a minute without progress.
local function drive_motion(d, s)
  local pos = d.position
  if s.guard then
    local t = threat(s, d)
    if t then
      halt(d)
      t.x0, t.y0 = pos.x, pos.y
      finish_order(s.walk, "threat", t)
      clear_motion(s)
      t.seat = s.name
      push_event("threat", t)
      return
    end
  end
  if sqdist(pos, s.target) <= ARRIVE_DIST * ARRIVE_DIST then
    halt(d)
    finish_order(s.walk, "arrived", { x = pos.x, y = pos.y })
    clear_motion(s)
    push_event("arrived", { seat = s.name, x = pos.x, y = pos.y })
    return
  end
  if s.last_pos and sqdist(pos, s.last_pos) < 0.0004 then
    s.stuck = (s.stuck or 0) + 1
  else
    s.stuck = 0
    s.last_pos = { x = pos.x, y = pos.y }
  end
  if s.stuck >= STUCK_TICKS then
    local tx, ty = s.target.x, s.target.y
    halt(d)
    finish_order(s.walk, "blocked", { reason = "stuck", x = pos.x, y = pos.y })
    clear_motion(s)
    push_event("path_blocked", { seat = s.name, x = tx, y = ty, reason = "stuck" })
    return
  end
  d.walking_state = { walking = true, direction = heading(pos, s.target) }
end

-- What a capsule throws, read from its prototype. A robot capsule names the
-- combat robot it releases after the capsule, which is how the follower cap
-- is told apart.
local function capsule_throw(proto)
  local ca = proto.capsule_action
  if not (ca and ca.type == "throw") then
    error(proto.name .. " is not a thrown capsule")
  end
  local ap = ca.attack_parameters
  for _, action in pairs(ap.ammo_type and ap.ammo_type.action or {}) do
    for _, delivery in pairs(action.action_delivery or {}) do
      if delivery.type == "projectile" and delivery.projectile then
        local robot = prototypes.entity[(proto.name:gsub("%-capsule$", ""))]
        return { projectile = delivery.projectile, speed = delivery.starting_speed or 0.3,
                 range = ap.range or 25, cooldown = math.max(1, math.ceil(ap.cooldown or 30)),
                 robot = robot and robot.type == "combat-robot" or nil }
      end
    end
  end
  error(proto.name .. " throws no projectile")
end

-- One tick of a seat's throw order: one capsule per cooldown, launched from
-- the drone as the drone's own projectile, so released robots follow it.
local function drive_throw(d, s)
  local t = s.throw
  if game.tick < t.next then return end
  local inv = d.get_main_inventory()
  if not (inv and inv.get_item_count(t.item) > 0) then
    clear_throw(s, t.thrown > 0 and "out_of_items" or "no_items")
    return
  end
  if t.robot and #d.following_robots >= d.force.maximum_following_robot_count then
    clear_throw(s, "follower_cap")
    return
  end
  local target = t.x and { x = t.x, y = t.y } or d.position
  d.surface.create_entity{ name = t.projectile, position = d.position, source = d,
                           target = target, speed = t.speed, force = d.force }
  inv.remove{ name = t.item, count = 1 }
  t.thrown = t.thrown + 1
  t.next = game.tick + t.cooldown
  if t.thrown >= t.want then clear_throw(s, "done") end
end

-- Book the products of finished crafts, which is what fires craft-item
-- triggers. The engine already books the ingredients. A finished craft is a
-- drop in the queued count for its recipe since the last snapshot.
local function book_crafts(d, s)
  local now = {}
  for _, q in pairs(d.crafting_queue or {}) do now[q.recipe] = (now[q.recipe] or 0) + q.count end
  local stats
  for recipe, was in pairs(s.crafts or {}) do
    local done = was - (now[recipe] or 0)
    local r = done > 0 and prototypes.recipe[recipe]
    if r then
      stats = stats or d.force.get_item_production_statistics(d.surface)
      for _, prod in pairs(r.products) do
        if prod.type == "item" then
          stats.on_flow(prod.name, done * (prod.amount or (prod.amount_min + prod.amount_max) / 2))
        end
      end
    end
  end
  s.crafts = now
end

-- A step longer than the drone can run is a teleport, which agent code cannot
-- make. Admin moves clear `last_xy` first.
local function watch_speed(d, s)
  local pos = d.position
  local last = s.last_xy
  if last and last.surface == d.surface.index and not d.vehicle then
    local step = math.sqrt(sqdist(pos, last))
    local limit = math.max(d.character_running_speed or 0, 0.15) * SPEED_SLACK + 0.01
    if step > limit then
      push_event("speed_violation", { seat = s.name, step = step, limit = limit,
                                      from = { x = last.x, y = last.y }, to = pos })
    end
  end
  s.last_xy = { x = pos.x, y = pos.y, surface = d.surface.index }
end

-- Give a seat a new drone at `pos`, or at its force's spawn.
local function spawn_drone(s, surf, pos)
  local force = game.forces[s.force or "player"]
  surf = surf or game.surfaces[s.surface or DEFAULT_SURFACE]
  if not (force and surf) then error("seat " .. s.name .. " has no such force or surface") end
  pos = pos or force.get_spawn_position(surf)
  surf.request_to_generate_chunks(pos, 1)
  surf.force_generate_chunk_requests()
  local d = surf.create_entity{ name = DRONE, position = pos, force = force }
  if not d then error("could not create a drone at (" .. pos.x .. "," .. pos.y .. ")") end
  if s.color then d.color = s.color end
  s.drone, s.unit, s.surface = d, d.unit_number, surf.name
  s.respawn_at, s.last_xy, s.crafts = nil, nil, nil
  return d
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

-- An error in on_tick is fatal to the whole server, so a seat whose order
-- cannot be driven loses the order instead.
local function drive_seat(s)
  local d = s.drone
  if not (d and d.valid) then
    if s.target or s.mining or s.throw then clear_orders(s, "lost") end
    if s.respawn_at and game.tick >= s.respawn_at then
      local ok, err = pcall(spawn_drone, s)
      if ok then
        push_event("respawned", { seat = s.name, position = s.drone.position })
      else
        s.respawn_at = game.tick + RESPAWN_TICKS
        push_event("respawn_failed", { seat = s.name, error = tostring(err) })
      end
    end
    return
  end
  local ok, err = pcall(function()
    watch_speed(d, s)
    if s.mining then drive_mining(d, s) end
    if s.target then drive_motion(d, s) end
    if s.throw then drive_throw(d, s) end
    if game.tick % CRAFT_TICKS == 0 and (s.crafts or d.crafting_queue) then book_crafts(d, s) end
    if game.tick % CHART_TICKS == 0 then
      local p = d.position
      d.force.chart(d.surface, { { p.x - CHART_RADIUS, p.y - CHART_RADIUS },
                                 { p.x + CHART_RADIUS, p.y + CHART_RADIUS } })
    end
  end)
  if not ok then
    pcall(halt, d)
    clear_orders(s, "error")
    push_event("order_dropped", { seat = s.name, error = tostring(err) })
  end
end

script.on_event(defines.events.on_tick, function()
  for _, s in pairs(storage.seats) do drive_seat(s) end
  if game.tick % WAIT_STEP == 0 and next(storage.waits) then
    local ok, err = pcall(check_waits)
    if not ok then
      for id in pairs(storage.waits) do finish_order(id, "error", { error = tostring(err) }) end
      storage.waits = {}
    end
  end
  if game.tick % 600 == 0 then pcall(prune_orders) end
end)

-- ---- lifecycle events ------------------------------------------------------

-- The engine leaves the corpse with the drone's inventory; the seat waits out
-- RESPAWN_TICKS for a new body.
script.on_event(defines.events.on_entity_died, function(e)
  local unit = e.entity.unit_number
  for _, s in pairs(storage.seats) do
    if s.unit == unit then
      clear_orders(s, "died")
      s.drone, s.crafts, s.last_xy = nil, nil, nil
      s.respawn_at = game.tick + RESPAWN_TICKS
      push_event("died", { seat = s.name, respawn_at = s.respawn_at,
                           cause = e.cause and e.cause.valid and e.cause.name or nil })
      return
    end
  end
end, { { filter = "name", name = DRONE } })

script.on_event(defines.events.on_player_joined_game, function(e)
  local p = game.players[e.player_index]
  push_event("player_joined", { player = p and p.name, index = e.player_index })
end)

script.on_event(defines.events.on_player_left_game, function(e)
  local p = game.players[e.player_index]
  push_event("player_left", { player = p and p.name, index = e.player_index })
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

local function seat_summary(s)
  local d = s.drone
  local alive = d ~= nil and d.valid
  return {
    seat = s.name,
    alive = alive,
    position = alive and d.position or nil,
    surface = alive and d.surface.name or s.surface,
    health = alive and d.health or nil,
    force = s.force or "player",
    respawn_at = s.respawn_at,
  }
end

local function player_summary(p)
  local ch = p.character
  return {
    player = p.name,
    index = p.index,
    connected = p.connected,
    controller = p.controller_type,
    position = ch and ch.valid and ch.position or nil,
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
  order = { 1, 2 ^ 53 }, unit_number = { 1, 2 ^ 53 }, guard_radius = { 0, 64 }, guard_hp = { 0, 1 },
}
local STR_MAX = {
  seat = 64, name = 128, recipe = 128, surface = 64, target = 128, search = 128,
  id = 64, file = 128, label = 256, direction = 32, item = 128, cond = 16,
  data = BP_CHUNK_MAX, blueprint = 4 * 1024 * 1024, code = SCRIPT_MAX,
  kind = 16, effect = 16, summary = 256, force = 64, camera = 64, type = 16, player = 64,
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

-- ---- the tool registry -----------------------------------------------------
--
-- A tool is a stored script with a name, a kind and an effect, kept in
-- `storage.tools` apart from the scripts so the script LRU never evicts it.
-- A `call` tool is a function body run with `args` as its `...`. A `lib` tool
-- runs once per peer and returns a table of functions that other tools fetch
-- with `use(name)`. TOOL_COMPILED and LIBS are per-peer caches, rebuilt on
-- demand and never saved.
--
-- A lib body must not make the peers differ. It sees `storage` through a proxy
-- that raises on any write, and a cached lib is reused only within the tick it
-- ran in, so every peer re-runs the bodies at the same tick.
--
-- DENY holds the effects the running tool_call refuses. It only ever grows
-- while calls nest, and the outermost call restores it on every path.

local TOOL_COMPILED = {}
local LIBS = {}
local LIB_LOADING = {}
local DENY = {}
local LAST_ERROR_MAX = 300

local write_blocked

local function wrapped_next(real, why)
  return function(_, k)
    local nk, v = next(real, k)
    if type(v) == "table" then v = write_blocked(v, why) end
    return nk, v
  end, nil, nil
end

-- A read-only view of a table; a write raises `why`.
write_blocked = function(real, why)
  why = why or "a lib tool must not write storage: its body runs on every peer at a different moment"
  return setmetatable({}, {
    __index = function(_, k)
      local v = real[k]
      if type(v) == "table" then return write_blocked(v, why) end
      return v
    end,
    __newindex = function() error(why, 2) end,
    __pairs = function() return wrapped_next(real, why) end,
    __ipairs = function()
      return function(_, i)
        i = i + 1
        local v = real[i]
        if v == nil then return nil end
        if type(v) == "table" then v = write_blocked(v, why) end
        return i, v
      end, nil, 0
    end,
    __len = function() return #real end,
    __metatable = false,
  })
end

-- ---- the realm guard -------------------------------------------------------
--
-- Under realm rules agent code sees the game through read-only proxies. A
-- property read, or a call to a method named like a query, passes through and
-- its result is guarded in turn. Any write, and any other method, raises an
-- error naming it. What agent code may change, it changes through `act`: the
-- calling seat's drone verbs, held to the same rules as the seat's own calls.
--
-- A proxy's real object is a key in REAL, a weak table that agent code cannot
-- reach without `debug`, which the guarded environment leaves out.

local READ_PREFIXES = { "get_", "find_", "count_", "can_", "is_", "has_" }
local READ_METHODS = { print = true, help = true, table_to_json = true, json_to_table = true,
                       encode_string = true, decode_string = true,
                       evaluate_expression = true, compare_versions = true,
                       to_be_deconstructed = true, to_be_upgraded = true, export_stack = true,
                       supports_bar = true, supports_filters = true, line_equals = true,
                       entity_prototype_collides = true, decorative_prototype_collides = true,
                       calculate_tile_properties = true }
local WRITE_METHODS = { get_or_create_control_behavior = true }
local READ_OK = {}   -- method name -> allowed; a pure cache

local REAL = setmetatable({}, { __mode = "k" })
local GUARD_MT = {}
local guard

local function readable(k)
  local ok = READ_OK[k]
  if ok == nil then
    ok = READ_METHODS[k] or false
    if not ok and not WRITE_METHODS[k] then
      for _, pre in ipairs(READ_PREFIXES) do
        if k:sub(1, #pre) == pre then ok = true break end
      end
    end
    READ_OK[k] = ok
  end
  return ok
end

local function object_name(real)
  local ok, name = pcall(function() return real.object_name end)
  return ok and name or type(real)
end

-- The real value behind agent data: proxies unwrap, plain tables are copied.
local function unguard(v, seen)
  if type(v) ~= "table" then return v end
  local real = REAL[v]
  if real ~= nil then return real end
  seen = seen or {}
  if seen[v] then return seen[v] end
  local out = {}
  seen[v] = out
  for k, x in pairs(v) do out[unguard(k, seen)] = unguard(x, seen) end
  return out
end

local function guard_all(...)
  local n = select("#", ...)
  if n == 1 then return guard((...)) end
  local t = { ... }
  for i = 1, n do t[i] = guard(t[i]) end
  return table.unpack(t, 1, n)
end

local function unguard_all(...)
  local n = select("#", ...)
  if n == 1 then return unguard((...)) end
  local t = { ... }
  for i = 1, n do t[i] = unguard(t[i]) end
  return table.unpack(t, 1, n)
end

-- Engine results are copies, so a plain table is copied with its values
-- guarded and stays writable; an engine object becomes a proxy.
guard = function(v)
  local t = type(v)
  if t == "userdata" then
    local p = setmetatable({}, GUARD_MT)
    REAL[p] = v
    return p
  elseif t == "table" and REAL[v] == nil then
    local out = {}
    for k, x in pairs(v) do out[k] = guard(x) end
    return out
  end
  return v
end

GUARD_MT.__index = function(p, k)
  local real = REAL[p]
  local v = real[k]
  if type(v) ~= "function" then return guard(v) end
  if not readable(k) then
    return function()
      error("realm rules: agent code may not call " .. object_name(real) .. "." .. k ..
            "; change the world through `act`", 2)
    end
  end
  return function(...) return guard_all(v(unguard_all(...))) end
end
GUARD_MT.__newindex = function(p, k)
  error("realm rules: agent code may not set " .. object_name(REAL[p]) .. "." .. tostring(k), 2)
end
GUARD_MT.__len = function(p) return #REAL[p] end
GUARD_MT.__eq = function(a, b) return REAL[a] == REAL[b] end
GUARD_MT.__tostring = function(p) return "guarded " .. object_name(REAL[p]) end
GUARD_MT.__pairs = function(p)
  local f, s, k0 = pairs(REAL[p])
  return function(_, k)
    local nk, nv = f(s, k)
    if nk == nil then return nil end
    return nk, guard(nv)
  end, nil, k0
end
GUARD_MT.__ipairs = function(p)
  local real = REAL[p]
  return function(_, i)
    i = i + 1
    if i > #real then return nil end
    return i, guard(real[i])
  end, nil, 0
end
GUARD_MT.__metatable = false

-- Guarded code writes its own storage, but only plain data: a proxy or a
-- function in `storage` would make the save fail.
local function check_storable(v, seen)
  local t = type(v)
  if t == "function" or t == "thread" then error("storage cannot hold a " .. t, 3) end
  if t ~= "table" then return end
  if REAL[v] ~= nil or getmetatable(v) ~= nil then
    error("storage holds plain data only, not a guarded object; keep its unit_number or position", 3)
  end
  seen = seen or {}
  if seen[v] then return end
  seen[v] = true
  for k, x in pairs(v) do check_storable(k, seen) check_storable(x, seen) end
end

local function checked_store(real)
  return setmetatable({}, {
    __index = function(_, k)
      local v = real[k]
      if type(v) == "table" then return checked_store(v) end
      return guard(v)
    end,
    __newindex = function(_, k, v) check_storable(v) real[k] = v end,
    __pairs = function()
      return function(_, k)
        local nk, v = next(real, k)
        if type(v) == "table" then v = checked_store(v) else v = guard(v) end
        return nk, v
      end, nil, nil
    end,
    __len = function() return #real end,
    __metatable = false,
  })
end

local READ_ONLY_LIB = "realm rules: library tables are read-only"
local GUARDED_LIBS = {
  string = write_blocked(string, READ_ONLY_LIB), table = write_blocked(table, READ_ONLY_LIB),
  math = write_blocked(math, READ_ONLY_LIB), serpent = write_blocked(serpent, READ_ONLY_LIB),
  defines = write_blocked(defines, READ_ONLY_LIB),
  bit32 = bit32 and write_blocked(bit32, READ_ONLY_LIB),
}
local GUARDED_FNS = { "assert", "error", "ipairs", "next", "pairs", "pcall", "xpcall", "select",
                      "tonumber", "tostring", "type", "rawequal", "rawlen", "rawget", "rawset",
                      "setmetatable", "log", "print", "table_size" }

-- The environment of guarded agent code: only what is listed here. Nothing
-- falls through to the mod's own globals.
local function guarded_env(storage_view, use_fn, act)
  local env = { storage = storage_view, use = use_fn, act = act,
                game = guard(game), prototypes = guard(prototypes), helpers = guard(helpers),
                script = guard(script), settings = guard(settings), rcon = guard(rcon) }
  for k, v in pairs(GUARDED_LIBS) do env[k] = v end
  for _, k in ipairs(GUARDED_FNS) do env[k] = _G[k] end
  env.getmetatable = function(t) if type(t) == "table" then return getmetatable(t) end end
  env.load = function(chunk, name, _, chunk_env) return load(chunk, name, "t", chunk_env or env) end
  env._G = env
  return setmetatable(env, { __metatable = false })
end

-- The environment of an unguarded tool body: `_G` is the environment itself,
-- so neither `_G.storage` nor the metatable reaches the real `storage`.
local function tool_env(storage_view, use_fn, act)
  local env = { storage = storage_view, script = SCRIPT_PROXY, use = use_fn, act = act }
  env._G = env
  return setmetatable(env, {
    __index = function(_, k)
      if k ~= "storage" and k ~= "_G" and k ~= "package" then return _G[k] end
    end,
    __metatable = false,
  })
end

local function compile_tool(name, rec)
  local c = TOOL_COMPILED[name]
  if c and c.code == rec.code then return c.fn end
  local fn, err = load("local _ENV=(...) return (function(...) " .. rec.code ..
                       "\nend)(select(2,...))", "=" .. name, "t")
  if not fn then error("tool " .. name .. " does not compile: " .. tostring(err), 0) end
  TOOL_COMPILED[name] = { code = rec.code, fn = fn }
  return fn
end

-- GUARDED is true while a guarded call runs, so the libs it uses are built
-- in the guarded environment too and cached apart from unguarded builds.
local GUARDED = false
local use

local function lib_env()
  if GUARDED then return guarded_env(write_blocked({}), use) end
  return tool_env(write_blocked(storage), use)
end

-- Run a lib body write-blocked and return the table it yields.
local function run_lib(name, rec)
  if LIB_LOADING[name] then error("lib cycle through " .. name, 0) end
  LIB_LOADING[name] = true
  local ok, r = pcall(function()
    local lib = compile_tool(name, rec)(lib_env())
    if type(lib) ~= "table" then error("lib " .. name .. " must return a table, got " .. type(lib), 0) end
    return lib
  end)
  LIB_LOADING[name] = nil
  if not ok then error(r, 0) end
  return r
end

use = function(name)
  if LIB_LOADING[name] then error("lib cycle through " .. tostring(name), 2) end
  local rec = storage.tools[name]
  if not rec then error("unknown tool: " .. tostring(name), 2) end
  if rec.kind ~= "lib" then error("tool " .. name .. " is not a lib", 2) end
  if DENY[rec.effect] then
    error({ denied = rec.effect .. " tool " .. name .. " refused" }, 0)
  end
  local key = GUARDED and name .. "#guarded" or name
  local cached = LIBS[key]
  if cached and cached.code == rec.code and cached.tick == game.tick then return cached.lib end
  local lib = run_lib(name, rec)
  LIBS[key] = { code = rec.code, lib = lib, tick = game.tick }
  return lib
end

local function forget_tool(name)
  TOOL_COMPILED[name] = nil
  LIBS = {}   -- other libs may hold a table from the old one
end

-- The calling seat's drone verbs, for agent code that chains actions.
local ACT_VERBS = { "walk", "stop", "mine", "craft", "build", "ghost", "unghost", "insert", "take",
                    "throw", "wait", "observe", "order_status", "recipes" }
local api

local function act_for(seat)
  if not seat then return nil end
  local verbs = {}
  for _, verb in ipairs(ACT_VERBS) do
    verbs[verb] = function(a)
      local args = type(a) == "table" and unguard(a) or {}
      args.seat = seat
      return api[verb](check_args(args))
    end
  end
  -- The seat's own body, to read. Nil while it waits to respawn.
  verbs.drone = function()
    local s = storage.seats[seat]
    local d = s and s.drone
    if not (d and d.valid) then return nil end
    return GUARDED and guard(d) or d
  end
  return write_blocked(verbs, "act is read-only")
end

api = {

  -- ---- registry ----

  -- Every drone seat and every player in the world. The realm service holds
  -- the authoritative seat table; this is the world's own view of it.
  seats = function()
    local seats, players = {}, {}
    for _, s in pairs(storage.seats) do seats[#seats + 1] = seat_summary(s) end
    for _, p in pairs(game.players) do players[#players + 1] = player_summary(p) end
    return { seats = seats, players = players }
  end,

  -- Make sure a seat has a drone, creating the seat on first call. x,y puts
  -- the drone there: precise placement is a deliberate cheat, so it is
  -- flagged. Without x,y a new drone appears at its force's spawn.
  spawn = function(args)
    local name = args.seat
    if not (name and name:match("^[%w%-_%.]+$")) then
      error("spawn needs 'seat', a name of letters, digits, - _ .")
    end
    if args.force and not game.forces[args.force] then error("no such force: " .. args.force) end
    local s = storage.seats[name]
    if not s then
      s = { name = name, force = args.force }
      storage.seats[name] = s
    end
    local surf = args.surface and game.surfaces[args.surface]
    if args.surface and not surf then error("no such surface: " .. args.surface) end
    local pos = args.x ~= nil and args.y ~= nil and { x = args.x, y = args.y } or nil
    local d, created = s.drone, false
    if not (d and d.valid) then
      d = spawn_drone(s, surf, pos)
      created = true
    elseif pos then
      d.teleport(pos, surf)
      s.last_xy, s.surface = nil, d.surface.name
    end
    clear_orders(s)
    if pos then
      push_event("placed", { seat = name, x = pos.x, y = pos.y, surface = d.surface.name,
                             cheated = true })
    end
    return { ok = true, seat = name, created = created, position = d.position,
             surface = d.surface.name }
  end,

  -- A seat's drone entity, for scenario-context scripts. Nil while it waits
  -- to respawn. Not JSON: call it through remote.call, never over the iface.
  drone = function(args)
    local d = resolve_seat(args).drone
    return d and d.valid and d or nil
  end,

  -- Park the camera client: a spectator with no character, so it draws the
  -- world and changes nothing the world simulates.
  camera = function(args)
    local p = game.get_player(args.player or "")
    if not p then return { ok = false, error = "not_found" } end
    local body = p.character
    if p.controller_type ~= defines.controllers.spectator then
      p.set_controller{ type = defines.controllers.spectator }
    end
    if body and body.valid then body.destroy() end
    return { ok = true, player = p.name, controller = p.controller_type }
  end,

  -- Remove a seat and its drone. The drone's inventory goes with it.
  despawn = function(args)
    local s = resolve_seat(args)
    clear_orders(s, "left")
    if s.drone and s.drone.valid then s.drone.destroy() end
    storage.seats[s.name] = nil
    return { ok = true, seat = s.name }
  end,

  -- ---- movement ----

  stop = function(args)
    local s = resolve_seat(args)
    clear_orders(s)
    local d = s.drone
    if d and d.valid then
      halt(d)
      d.mining_state = { mining = false }
    end
    return { ok = true, seat = s.name }
  end,

  -- Teleport a seat. Separate from `spawn` and honest about what it is: the
  -- precision escape hatch, never how an agent is meant to travel.
  teleport = function(args)
    local s, d = acting_seat(args)
    if args.x == nil or args.y == nil then error("teleport requires x and y") end
    local surf = args.surface and game.surfaces[args.surface] or d.surface
    if not surf then error("no such surface: " .. tostring(args.surface)) end
    clear_orders(s)
    if not d.teleport({ x = args.x, y = args.y }, surf) then
      error(string.format("cannot teleport to (%.1f,%.1f)", args.x, args.y))
    end
    s.last_xy, s.surface = nil, surf.name
    push_event("placed", { seat = s.name, x = args.x, y = args.y,
                           surface = surf.name, cheated = true })
    return { ok = true, seat = s.name, position = d.position,
             surface = surf.name, cheated = true }
  end,

  -- ---- gameplay ----

  -- Real mining: hand the engine a mining_state and let it run. The tick driver
  -- re-asserts it and reports what actually landed in the drone's inventory.
  -- No script decrements the resource -- the engine does, at the game's own rate.
  mine = function(args)
    local s, ch = acting_seat(args)
    if args.x == nil or args.y == nil then error("mine requires x and y") end
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
    clear_mining(s)
    s.mining = {
      position = { x = target.position.x, y = target.position.y },
      entity = target,
      product = product,
      base = product and main_inventory_count(ch, product) or 0,
      want = args.count,
      idle = 0,
    }
    return { ok = true, seat = s.name, mining = target.name, product = product,
             want = args.count, position = s.mining.position }
  end,

  -- The real crafting queue: the engine plans the intermediates and reserves
  -- the ingredients now. The tick driver books each finished craft.
  craft = function(args)
    local s, d = acting_seat(args)
    if not args.recipe then error("craft requires 'recipe'") end
    local rec = d.force.recipes[args.recipe]
    if not rec then error("unknown recipe: " .. args.recipe) end
    if not rec.enabled then error("recipe not unlocked: " .. args.recipe) end
    book_crafts(d, s)
    local queued = d.begin_crafting{ recipe = args.recipe, count = args.count or 1 }
    book_crafts(d, s)
    if queued == 0 then
      error("cannot craft " .. args.recipe .. " (missing ingredients?)")
    end
    return { ok = true, seat = s.name, queued = queued }
  end,

  build = function(args)
    local s, ch = acting_seat(args)
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
      raise_built = true }
    if not ent then error("placement failed") end
    inv.remove{ name = args.name, count = 1 }   -- only after success
    return { ok = true, seat = s.name, built = ename, position = ent.position }
  end,

  -- Plan an entity as a ghost for robots to build, as a player places one
  -- from the map: anywhere the force may build, no reach needed. `recipe`
  -- presets a machine's recipe; `type` is an underground belt's input/output.
  ghost = function(args)
    local s, d = acting_seat(args)
    local proto = args.name and prototypes.entity[args.name]
    if not (proto and proto.items_to_place_this) then
      error("ghost needs 'name', an entity an item places (got " .. tostring(args.name) .. ")")
    end
    if args.x == nil or args.y == nil then error("ghost requires x and y") end
    local dir = defines.direction.north
    if args.direction then
      dir = defines.direction[args.direction] or tonumber(args.direction)
      if dir == nil then error("bad direction: " .. tostring(args.direction)) end
    end
    local surf = args.surface and game.surfaces[args.surface] or d.surface
    if not surf then error("no such surface: " .. tostring(args.surface)) end
    local pos = { x = args.x, y = args.y }
    if not surf.can_place_entity{ name = args.name, position = pos, direction = dir, force = d.force,
                                  build_check_type = defines.build_check_type.manual_ghost } then
      error(string.format("cannot place a ghost of %s at (%.1f,%.1f)", args.name, pos.x, pos.y))
    end
    local g = surf.create_entity{ name = "entity-ghost", inner_name = args.name, position = pos,
                                  direction = dir, force = d.force, type = args.type }
    if not g then error("the engine refused the ghost of " .. args.name) end
    if args.recipe then
      local ok, why = pcall(g.set_recipe, args.recipe)
      if not ok then
        g.destroy()
        error("recipe " .. args.recipe .. ": " .. tostring(why))
      end
    end
    return { ok = true, seat = s.name, unit_number = g.unit_number, position = g.position,
             direction = g.direction }
  end,

  -- Cancel one of the force's ghosts by unit number.
  unghost = function(args)
    local _, d = acting_seat(args)
    local g = args.unit_number and game.get_entity_by_unit_number(args.unit_number)
    if not (g and g.valid and (g.type == "entity-ghost" or g.type == "tile-ghost")) then
      error("no ghost with unit_number " .. tostring(args.unit_number))
    end
    if g.force ~= d.force then error("that ghost belongs to another force") end
    g.destroy()
    return { ok = true }
  end,

  insert = function(args)
    local s, ch = acting_seat(args)
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
    return { ok = true, seat = s.name, inserted = put, target = target.name }
  end,

  take = function(args)
    local s, ch = acting_seat(args)
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
    return { ok = true, seat = s.name, taken = kept, from = target.name }
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
  -- those. Asking the drone itself is the only answer that accounts for both
  -- the crafting category and what is in its hands right now.
  recipes = function(args)
    args = args or {}
    local force = game.forces.player
    local limit = args.limit or 40
    local s = args.seat and storage.seats[args.seat]
    local d = s and s.drone and s.drone.valid and s.drone or nil
    local out = {}
    for name, rec in pairs(force.recipes) do
      if rec.enabled and not rec.hidden
         and (not args.search or string.find(name, args.search, 1, true)) then
        local ing, prod = {}, {}
        for _, i in pairs(rec.ingredients) do
          ing[#ing + 1] = { name = i.name, amount = i.amount, type = i.type }
        end
        for _, pr in pairs(rec.products) do
          prod[#prod + 1] = { name = pr.name, amount = pr.amount or pr.amount_max,
                              type = pr.type }
        end
        local row = { name = name, ingredients = ing, products = prod }
        if d then row.craftable = d.get_craftable_count(name) end
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
    -- A seatless stamp needs a surface and works on a lab with no player.
    local s, d
    if args.seat then s, d = acting_seat(args) end
    local force
    if d then
      force = d.force
    else
      if not args.surface then error("a stamp without a seat requires 'surface'") end
      force = game.forces[args.force or "player"]
      if not force then error("no such force: " .. tostring(args.force)) end
    end
    local id = tostring(args.id or "default")
    local text = args.blueprint
    if not text then
      local buf = (storage.bp or {})[id]
      if not buf or #buf == 0 then error("no blueprint uploaded for id " .. id) end
      text = table.concat(buf)
    end
    if args.x == nil or args.y == nil then error("stamp requires x and y") end
    local surf = args.surface and game.surfaces[args.surface] or (d and not args.surface and d.surface)
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
      local plan = stamp_plan(stack.get_blueprint_entities() or {}, args.x, args.y, dir)
      -- Find every obstruction BEFORE building: one of them means nothing at
      -- all is placed, so an unexplained zero has to come back with a reason.
      local blocked, blocked_at, cleared = 0, {}, 0
      -- Clear every footprint first, so no entity is counted blocked by a
      -- rock that a neighbour's clearing removes later.
      if args.clear then
        for _, e in pairs(plan) do
          for _, obstruction in pairs(surf.find_entities_filtered{
                area = { { e.x - e.w / 2, e.y - e.h / 2 }, { e.x + e.w / 2, e.y + e.h / 2 } },
                type = { "tree", "simple-entity" } }) do
            if obstruction.valid then obstruction.destroy() cleared = cleared + 1 end
          end
        end
      end
      for _, e in pairs(plan) do
        local px, py = e.x, e.y
        if not surf.can_place_entity{
              name = e.name, position = { x = px, y = py }, force = force,
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
      if #plan > 0 then
        local x1, y1 = math.huge, math.huge
        local x2, y2 = -math.huge, -math.huge
        for _, e in pairs(plan) do
          x1 = math.min(x1, e.x); x2 = math.max(x2, e.x)
          y1 = math.min(y1, e.y); y2 = math.max(y2, e.y)
        end
        corners = { { x = x1, y = y1 }, { x = x2, y = y1 }, { x = x1, y = y2 }, { x = x2, y = y2 } }
      end
      local ghosts = stack.build_blueprint{
        surface = surf, force = force, position = { x = args.x, y = args.y },
        direction = dir, skip_fog_of_war = false,
        build_mode = args.force_build and defines.build_mode.forced or defines.build_mode.normal,
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
      push_event("blueprint_partial", { seat = s and s.name, placed = result.placed,
                                        skipped = skipped,
                                        ungenerated = not generated })
    end
    return { ok = true, seat = s and s.name, position = { x = args.x, y = args.y },
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
    local s = args.seat and resolve_seat(args)
    local c = s and s.drone and s.drone.valid and s.drone or nil
    local surf = args.surface and game.surfaces[args.surface]
                 or (c and c.surface)
                 or game.surfaces[1]
    local area
    if args.radius then
      if not (c or (args.x and args.y)) then error("radius needs x,y or a seat with a drone") end
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
    local file = args.file or string.format("bp-%s-%d.txt", s and s.name or "world", game.tick)
    local inv = game.create_inventory(1)
    local ok, result = pcall(function()
      local stack = inv[1]
      stack.set_stack{ name = "blueprint" }
      stack.create_blueprint{
        surface = surf, force = c and c.force or game.forces[args.force or "player"], area = area,
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
    return { ok = true, seat = s and s.name, file = file, entities = result,
             area = { x1 = area[1][1], y1 = area[1][2],
                      x2 = area[2][1], y2 = area[2][2] },
             surface = surf.name }
  end,

  -- ---- vision ----

  -- The server cannot render, so a screenshot is drawn by ONE connected
  -- client -- `camera`, a player name, or else the first spectator -- and
  -- written inside that client's container, whose script-output the service
  -- reads. The engine returns before the file exists, so the caller waits on
  -- the file, not on this. The frame centres on x,y, or on the seat's drone.
  -- Resolution is rendered offscreen, so a camera's window size does not bound it.
  screenshot = function(args)
    local by
    if args.camera then
      by = game.players[args.camera]
      if not (by and by.connected) then error("camera " .. args.camera .. " is not connected") end
    else
      for _, q in pairs(game.connected_players) do
        if q.controller_type == defines.controllers.spectator then by = q break end
      end
      by = by or game.connected_players[1]
      if not by then error("no client is connected to draw a screenshot; start a camera") end
    end
    local s = args.seat and resolve_seat(args)
    local d = s and s.drone and s.drone.valid and s.drone or nil
    local pos
    if args.x ~= nil and args.y ~= nil then
      pos = { x = args.x, y = args.y }
    elseif d then
      pos = { x = d.position.x, y = d.position.y }
    else
      error("screenshot needs x and y, or a seat with a drone")
    end
    local surf = args.surface and game.surfaces[args.surface] or (d and d.surface) or game.surfaces[1]
    if not surf then error("no such surface: " .. tostring(args.surface)) end
    local file = args.file or
        string.format("%s-%d.png", s and s.name or by.name, game.tick)
    game.take_screenshot{
      by_player = by,
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
    return { ok = true, seat = s and s.name, camera = by.name, file = file, position = pos,
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
    local s = acting_seat(args)
    if args.x == nil or args.y == nil then error("walk requires x and y") end
    clear_motion(s, "superseded")
    s.target = { x = args.x, y = args.y }
    if args.guard ~= false then
      s.guard = { radius = args.guard_radius or GUARD_RADIUS,
                  hp = args.guard_hp or GUARD_HP,
                  worms = args.guard_worms ~= false }
    end
    local o = new_order("walk", s.name, { target = s.target })
    s.walk = o.id
    return { ok = true, order = o.id, seat = s.name, target = s.target, guard = s.guard }
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
        o.entity = select(2, acting_seat(args))
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

  -- Throw `count` capsules from the drone's own inventory, one per capsule
  -- cooldown, at x,y (within the capsule's range) or at the drone's feet.
  throw = function(args)
    local s, d = acting_seat(args)
    local proto = args.item and prototypes.item[args.item]
    if not (proto and proto.type == "capsule") then
      error("throw needs 'item', a capsule (got " .. tostring(args.item) .. ")")
    end
    if (args.x == nil) ~= (args.y == nil) then error("throw takes both x and y, or neither") end
    local how = capsule_throw(proto)
    if args.x and sqdist(d.position, args) > how.range * how.range then
      error(string.format("(%.1f,%.1f) is beyond %s range %g", args.x, args.y, args.item, how.range))
    end
    if how.robot and #d.following_robots >= d.force.maximum_following_robot_count then
      error("seat " .. s.name .. " already has " .. #d.following_robots .. " following robots, the cap")
    end
    clear_throw(s, "superseded")
    local want = math.max(1, args.count or 1)
    local o = new_order("throw", s.name, { item = args.item, want = want })
    s.throw = { id = o.id, item = args.item, want = want, thrown = 0, next = game.tick,
                cooldown = how.cooldown, projectile = how.projectile, speed = how.speed,
                robot = how.robot, x = args.x, y = args.y }
    return { ok = true, order = o.id, seat = s.name, cooldown = how.cooldown }
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
  -- `guarded` runs it under realm rules, with `act` for `seat`.
  script_run = function(args)
    local fn = compiled(args.name)
    storage.scripts[args.name].used = game.tick
    local env
    if args.guarded == true then
      env = guarded_env(checked_store(storage.script_data), nil, act_for(args.seat))
    else
      env = setmetatable({ storage = storage.script_data, script = SCRIPT_PROXY,
                           act = act_for(args.seat) }, { __index = _G })
    end
    local outer = GUARDED
    GUARDED = args.guarded == true
    local ok, r = pcall(fn, env, args.args)
    GUARDED = outer
    if not ok then error(r, 0) end
    return r
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

  -- ---- tools ----

  tool_define = function(args)
    local name, code, kind, effect = args.name, args.code, args.kind, args.effect
    if not (name and name:match("^[%w_%-]+$") and #name <= 64) then
      error("tool name must be 1-64 letters, digits, - _")
    end
    if not code then error("tool_define requires 'code'") end
    if kind ~= "call" and kind ~= "lib" then error("tool kind must be call or lib") end
    if effect ~= "read" and effect ~= "act" and effect ~= "cheat" then
      error("tool effect must be read, act or cheat")
    end
    if not storage.tools[name] and table_size(storage.tools) >= TOOLS_CAP then
      error("the tool registry is full (" .. TOOLS_CAP .. "); drop a tool first")
    end
    local rec = { code = code, kind = kind, effect = effect, summary = args.summary or "",
                  defined = game.tick, calls = 0, errors = 0 }
    TOOL_COMPILED[name] = nil
    LIBS = {}   -- a dependent lib must re-run against the definition being tried
    local lib
    local ok, err = pcall(function()
      compile_tool(name, rec)
      if kind == "lib" then lib = run_lib(name, rec) end
    end)
    if not ok then
      TOOL_COMPILED[name] = nil
      error(err, 0)
    end
    storage.tools[name] = rec
    forget_tool(name)
    compile_tool(name, rec)
    if lib then LIBS[name] = { code = code, lib = lib, tick = game.tick } end
    return { name = name, size = #code }
  end,

  -- Run a call tool with `args` as its `...`. It sees the game's globals,
  -- `use`, the restricted `script`, and `storage` bound to a table of its own.
  -- `deny` lists effects to refuse for this call and every call nested in it,
  -- the tool itself and each lib it uses. `guarded` runs it under realm
  -- rules: a read tool gets the guarded view, an act tool also gets `act`
  -- for `seat`, and a cheat tool is refused.
  tool_call = function(args)
    local name = args.name
    local rec = storage.tools[name or ""]
    if not rec then return { ok = false, error = "no_tool", detail = tostring(name) } end
    local outer, deny = DENY, {}
    for e in pairs(outer) do deny[e] = true end
    if args.deny ~= nil and type(args.deny) ~= "table" then
      error("argument deny must be a list of effects")
    end
    for _, e in ipairs(args.deny or {}) do deny[e] = true end
    if deny[rec.effect] then
      return { ok = false, error = "denied",
               detail = rec.effect .. " tool " .. name .. " refused" }
    end
    if rec.kind ~= "call" then
      return { ok = false, error = "tool_error", detail = "lib tools are used, not called" }
    end
    local guarded = args.guarded == true or GUARDED
    if guarded and rec.effect == "cheat" then
      return { ok = false, error = "denied", detail = "cheat tool " .. name .. " refused under realm rules" }
    end
    local data = storage.tool_data[name]
    if not data then
      data = {}
      storage.tool_data[name] = data
    end
    local act = rec.effect ~= "read" and act_for(args.seat) or nil
    local env = guarded and guarded_env(checked_store(data), use, act) or tool_env(data, use, act)
    local outer_guarded = GUARDED
    DENY, GUARDED = deny, guarded and true or false
    local ok, r = pcall(function()
      return compile_tool(name, rec)(env, args.args)
    end)
    DENY, GUARDED = outer, outer_guarded
    if ok then
      rec.calls = rec.calls + 1
      return { ok = true, r = r }
    end
    if type(r) == "table" and r.denied then
      return { ok = false, error = "denied", detail = r.denied }
    end
    local detail = tostring(r):sub(1, LAST_ERROR_MAX)
    rec.errors = rec.errors + 1
    rec.last_error = detail
    return { ok = false, error = "tool_error", detail = detail }
  end,

  tool_list = function()
    local out = {}
    for n, rec in pairs(storage.tools) do
      out[#out + 1] = { name = n, kind = rec.kind, effect = rec.effect, summary = rec.summary,
                        size = #rec.code, calls = rec.calls, errors = rec.errors,
                        last_error = rec.last_error, defined = rec.defined }
    end
    table.sort(out, function(a, b) return a.name < b.name end)
    return { tools = out }
  end,

  tool_drop = function(args)
    local name = args.name or ""
    storage.tools[name] = nil
    storage.tool_data[name] = nil
    forget_tool(name)
    return { ok = true }
  end,

  drain_events = function()
    local ev = storage.events or {}
    storage.events = {}
    return { events = ev }
  end,

  observe = function(args)
    local s = resolve_seat(args)
    local out = {
      tick = game.tick,
      paused = game.tick_paused,
      seat = s.name,
      force = s.force or "player",
      body = false,
      respawn_at = s.respawn_at,
    }

    -- Who else is in the world. On one shared force everything below is
    -- everyone's, so an agent needs to know who it is sharing with.
    local others = {}
    for _, q in pairs(storage.seats) do
      if q ~= s then others[#others + 1] = seat_summary(q) end
    end
    out.seats = others
    local players = {}
    for _, p in pairs(game.connected_players) do players[#players + 1] = player_summary(p) end
    out.players = players

    local ch = s.drone
    if ch and ch.valid then
      out.body = true
      out.surface = ch.surface.name
      out.position = ch.position
      out.health = ch.health
      out.max_health = ch.max_health
      out.walking = ch.walking_state.walking
      out.target = s.target
      out.mining = s.mining and { x = s.mining.position.x, y = s.mining.position.y,
                                  item = s.mining.product } or nil
      out.inventory = main_inventory(ch)
      out.reach = ch.reach_distance
      out.following_robots = #ch.following_robots
      local q = {}
      for _, item in pairs(ch.crafting_queue or {}) do
        if not item.prerequisite then q[#q + 1] = { recipe = item.recipe, count = item.count } end
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
          -- Name the bodies. Without this a teammate reads as an anonymous
          -- "character" entity indistinguishable from scenery.
          if ent.type == "character" then
            if ent.player then
              rec.player = ent.player.name
            else
              for _, q2 in pairs(storage.seats) do
                if q2.drone == ent then rec.seat = q2.name break end
              end
            end
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

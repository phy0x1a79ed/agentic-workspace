-- The drone: the body an agent seat drives. A character, so it walks, mines,
-- hand-crafts, builds and fights by the game's own rules, but one that collides
-- with nothing and reaches as far as it sees. Copied in final-fixes so changes
-- other mods make to the base character carry over.

local RANGE = 128
local GFX = "__game-bot-control__/graphics/"
local SCALE = 1
local SHIFT = {0, -1.1}
local SHADOW_SHIFT = {1.2, 0.5}

local function sheet(name, directions)
  local sprite = {
    filename = GFX .. "drone-" .. name .. ".png",
    width = 66, height = 76,
    direction_count = directions, frame_count = 1, line_length = directions,
    shift = SHIFT, scale = SCALE,
  }
  local shadow = {
    filename = GFX .. "drone-" .. name .. "-shadow.png",
    width = 104, height = 49,
    direction_count = directions, frame_count = 1, line_length = directions,
    shift = SHADOW_SHIFT, scale = SCALE, draw_as_shadow = true,
  }
  return {layers = {sprite, shadow}}
end

local armors = {}
for name in pairs(data.raw.armor) do armors[#armors + 1] = name end
table.sort(armors)

local function animation(with_armors)
  local idle, running = sheet("idle", 8), sheet("running", 8)
  local running_gun = sheet("running-gun", 18)
  return {
    armors = with_armors,
    idle = idle, idle_with_gun = idle, mining_with_tool = idle,
    running = running, running_with_gun = running_gun,
    idle_in_air = idle, idle_with_gun_in_air = idle,
    flying = running, flying_with_gun = running_gun,
  }
end

local drone = table.deepcopy(data.raw.character.character)
drone.name = "game-bot-drone"
drone.localised_name = "Drone"
drone.collision_mask = {layers = {trigger_target = true}}
drone.crafting_categories = {"crafting", "hand-crafting"}
drone.mining_categories = {"basic-solid"}
drone.reach_distance = RANGE
drone.build_distance = RANGE
drone.reach_resource_distance = RANGE
drone.drop_item_distance = RANGE
drone.item_pickup_distance = RANGE
drone.loot_pickup_distance = RANGE
drone.selection_box = {{-0.6, -2}, {0.6, 0}}
drone.animations = {animation(nil), animation(#armors > 0 and armors or nil)}
drone.synced_footstep_particle_triggers = nil
drone.footstep_particle_triggers = nil
drone.footprint_particles = nil
drone.left_footprint_frames = nil
drone.right_footprint_frames = nil
drone.running_sound_animation_positions = {}
drone.moving_sound_animation_positions = {}
drone.mining_with_tool_particles_animation_positions = {0}
drone.water_reflection = nil

data:extend({drone})

---
name: Three.js DevTools MCP
type: tool
tags: [threejs, 3d, devtools, mcp]
description: threejs-devtools-mcp server tools for live scene inspection and modification
---

# Three.js DevTools MCP

## Connection

The `threejs-devtools-mcp` server connects to a running Three.js app via WebSocket bridge.

**Prerequisites:**
1. Vite dev server running (`pnpm dev`)
2. Browser tab open with the app
3. Bridge script included in `index.html`:
   ```html
   <script src="http://localhost:8080/bridge.js"></script>
   ```

## Key Tools

### Scene Inspection
- `get_scene_tree` — full scene graph hierarchy
- `get_object_properties(name)` — position, rotation, scale, material, geometry
- `get_renderer_info` — WebGL capabilities, draw calls, memory

### Screenshots
- `take_screenshot` — capture current viewport
- `take_screenshot({ width, height })` — custom resolution

### Performance
- `get_fps` — current frame rate
- `get_memory` — GPU memory usage (geometries, textures)

### Live Editing
- `set_material_property(name, property, value)` — change color, roughness, etc.
- `set_object_transform(name, { position, rotation, scale })` — reposition objects
- `set_uniform(materialName, uniformName, value)` — tweak shader uniforms

### Camera
- `get_camera_info` — position, target, FOV
- `set_camera({ position, lookAt })` — reposition camera

## Workflow: Inspect → Modify → Verify

1. `get_scene_tree` to understand structure
2. `take_screenshot` to see current state
3. `set_material_property` or `set_uniform` to adjust
4. `take_screenshot` to verify change
5. Update source code to make change permanent

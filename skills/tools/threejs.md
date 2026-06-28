---
name: Three.js
type: tool
tags: [threejs, 3d, glsl, shaders, presentation]
description: Three.js reference — setup, scene/camera/renderer, geometry, materials, lighting, shaders, textures, animation, interaction, loaders, post-processing, devtools MCP
---

# Three.js Reference

## Project Setup

```bash
pnpm create vite my-presentation -- --template vanilla
cd my-presentation
pnpm add three
pnpm dev
```

### WSL2 Vite Config

```js
// vite.config.js
import { defineConfig } from 'vite'
export default defineConfig({
  server: { host: '0.0.0.0', open: false },
  build: { outDir: 'dist' },
})
```

Access from Windows browser at `http://<WSL-IP>:5173` or `http://localhost:5173` if port forwarding is configured.

## Fundamentals

### Scene / Camera / Renderer

```js
import * as THREE from 'three'

const scene = new THREE.Scene()
const camera = new THREE.PerspectiveCamera(60, window.innerWidth / window.innerHeight, 0.1, 100)
const renderer = new THREE.WebGLRenderer({ antialias: true })
renderer.setSize(window.innerWidth, window.innerHeight)
renderer.setPixelRatio(window.devicePixelRatio)
document.body.appendChild(renderer.domElement)
```

### Production Renderer Settings

```js
renderer.shadowMap.enabled = true
renderer.shadowMap.type = THREE.PCFSoftShadowMap
renderer.toneMapping = THREE.ACESFilmicToneMapping
renderer.toneMappingExposure = 0.9

scene.fog = new THREE.FogExp2(0x000000, 0.035)
```

### Coordinate System

Right-handed: +X right, +Y up, +Z toward viewer. Units are arbitrary — 1 unit = 1 meter is a common convention.

### Resize Handler

```js
window.addEventListener('resize', () => {
  camera.aspect = window.innerWidth / window.innerHeight
  camera.updateProjectionMatrix()
  renderer.setSize(window.innerWidth, window.innerHeight)
})
```

## Geometry

### Built-ins

```js
new THREE.BoxGeometry(width, height, depth)
new THREE.SphereGeometry(radius, widthSegments, heightSegments)
new THREE.PlaneGeometry(width, height, widthSegments, heightSegments)
new THREE.IcosahedronGeometry(radius, detail)
new THREE.CylinderGeometry(radiusTop, radiusBottom, height, radialSegments)
new THREE.TorusGeometry(radius, tube, radialSegments, tubularSegments)
```

### Custom BufferGeometry

```js
const geo = new THREE.BufferGeometry()
const positions = new Float32Array([...]) // x,y,z triplets
geo.setAttribute('position', new THREE.BufferAttribute(positions, 3))
geo.computeVertexNormals()
```

### InstancedMesh (High-Performance Instancing)

```js
const mesh = new THREE.InstancedMesh(geometry, material, count)
const dummy = new THREE.Object3D()
for (let i = 0; i < count; i++) {
  dummy.position.set(x, y, z)
  dummy.updateMatrix()
  mesh.setMatrixAt(i, dummy.matrix)
}
mesh.instanceMatrix.needsUpdate = true
```

### Merging Geometries (Static Batching)

```js
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js'
const merged = mergeGeometries([geo1, geo2, geo3])
```

## Materials

### MeshStandardMaterial (PBR)

```js
new THREE.MeshStandardMaterial({
  color: 0x4488ff,
  roughness: 0.3,
  metalness: 0.6,
  map: texture,           // albedo
  normalMap: normalTex,
  roughnessMap: roughTex,
})
```

### MeshPhysicalMaterial (Extended PBR)

```js
new THREE.MeshPhysicalMaterial({
  clearcoat: 1.0,
  clearcoatRoughness: 0.1,
  transmission: 0.9,      // glass
  ior: 1.5,
  thickness: 0.5,
})
```

### Common Settings

- `transparent: true` + `opacity` or shader alpha for transparency
- `side: THREE.DoubleSide` for visible from both sides
- `depthWrite: false` for additive/overlay effects
- `blending: THREE.AdditiveBlending` for glow effects

## Lighting

### Light Types

```js
// Directional (sun-like, parallel rays)
const sun = new THREE.DirectionalLight(0xffffff, 2.0)
sun.position.set(5, 8, -3)

// Point (omnidirectional, falloff)
const point = new THREE.PointLight(0xff8800, 1.0, 20)

// Spot (cone-shaped)
const spot = new THREE.SpotLight(0xffffff, 1.0, 30, Math.PI / 6)

// Ambient (uniform, no direction)
const ambient = new THREE.AmbientLight(0x404040, 0.5)

// Hemisphere (sky + ground colors)
const hemi = new THREE.HemisphereLight(0x87ceeb, 0x362907, 0.3)
```

### Shadow Setup

```js
// Renderer
renderer.shadowMap.enabled = true
renderer.shadowMap.type = THREE.PCFSoftShadowMap

// Light
sun.castShadow = true
sun.shadow.mapSize.set(2048, 2048)
sun.shadow.camera.near = 0.5
sun.shadow.camera.far = 30
sun.shadow.camera.left = -10
sun.shadow.camera.right = 10
sun.shadow.camera.top = 10
sun.shadow.camera.bottom = -10
sun.shadow.bias = -0.001

// Objects
mesh.castShadow = true
floor.receiveShadow = true
```

### Atmospheric Pattern

Combine directional + ambient + hemisphere for depth:

```js
const sun = new THREE.DirectionalLight(0xfff4e0, 3.0)
const ambient = new THREE.AmbientLight(0x1a2a4a, 0.4)
const hemi = new THREE.HemisphereLight(0x2244aa, 0x0a0f1a, 0.3)
```

## Shaders (GLSL)

### ShaderMaterial

```js
new THREE.ShaderMaterial({
  uniforms: {
    uTime: { value: 0 },
    uResolution: { value: new THREE.Vector2(window.innerWidth, window.innerHeight) },
    uColor: { value: new THREE.Color(0xff0000) },
    uTexture: { value: texture },
  },
  vertexShader: /* glsl */ `
    varying vec2 vUv;
    void main() {
      vUv = uv;
      gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
    }
  `,
  fragmentShader: /* glsl */ `
    uniform float uTime;
    varying vec2 vUv;
    void main() {
      gl_FragColor = vec4(vUv, sin(uTime) * 0.5 + 0.5, 1.0);
    }
  `,
})
// Update in animate: material.uniforms.uTime.value = elapsed
```

Always use the `/* glsl */` tagged template for syntax highlighting.

### Built-in Uniforms (Available Automatically)

- `projectionMatrix` — camera projection
- `modelViewMatrix` — model * view
- `modelMatrix`, `viewMatrix` — separate
- `normalMatrix` — inverse transpose of modelView (for normals)
- `cameraPosition` — world-space camera position

### Built-in Attributes

- `position` (vec3), `normal` (vec3), `uv` (vec2)

### Simple Noise (No Dependencies)

```glsl
float hash(vec2 p) {
  return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
}
float noise(vec2 p) {
  vec2 i = floor(p), f = fract(p);
  f = f * f * (3.0 - 2.0 * f);
  return mix(
    mix(hash(i), hash(i + vec2(1,0)), f.x),
    mix(hash(i + vec2(0,1)), hash(i + vec2(1,1)), f.x),
    f.y
  );
}
```

### Fog Integration (Match Scene Fog)

```glsl
// Vertex
float fogDist = length(mvPos.xyz);
vFogFactor = 1.0 - exp(-uFogDensity * uFogDensity * fogDist * fogDist);

// Fragment
col = mix(col, uFogColor, vFogFactor);
```

## Textures

### Loading

```js
const loader = new THREE.TextureLoader()
const tex = loader.load('/assets/texture.png')
tex.colorSpace = THREE.SRGBColorSpace // for albedo/color maps
```

### Canvas Textures (Labels, Data Viz)

```js
const canvas = document.createElement('canvas')
canvas.width = 512
canvas.height = 512
const ctx = canvas.getContext('2d')
ctx.fillStyle = '#fff'
ctx.font = '24px sans-serif'
ctx.fillText('Label', 10, 40)

const tex = new THREE.CanvasTexture(canvas)
// Update: modify canvas, then set tex.needsUpdate = true
```

### Render Targets (Offscreen Rendering)

```js
const rt = new THREE.WebGLRenderTarget(1024, 1024)
renderer.setRenderTarget(rt)
renderer.render(offscreenScene, offscreenCamera)
renderer.setRenderTarget(null)
// Use rt.texture as input to materials
```

### Data Textures

```js
const width = 256, height = 256
const data = new Uint8Array(width * height * 4) // RGBA
const tex = new THREE.DataTexture(data, width, height, THREE.RGBAFormat)
tex.needsUpdate = true
```

## Animation

### THREE.Clock (Frame-Rate Independent)

```js
const clock = new THREE.Clock()
function animate() {
  requestAnimationFrame(animate)
  const delta = clock.getDelta()
  const elapsed = clock.getElapsedTime()
  material.uniforms.uTime.value = elapsed
  mixer?.update(delta) // for AnimationMixer
  controls?.update()
  renderer.render(scene, camera)
}
animate()
```

### AnimationMixer (glTF Animations)

```js
const mixer = new THREE.AnimationMixer(model)
const action = mixer.clipAction(model.animations[0])
action.play()
// In animate loop: mixer.update(delta)
```

### Tween Patterns

```js
object.position.lerp(target, 0.05)                      // smooth lerp
mesh.position.y = 1.5 + Math.sin(elapsed * 0.5) * 0.2   // sine oscillation
mesh.rotation.y = elapsed * 0.3                         // rotation
```

## Interaction

### OrbitControls

```js
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'

const controls = new OrbitControls(camera, renderer.domElement)
controls.enableDamping = true
controls.dampingFactor = 0.05
// In animate loop: controls.update()
```

### Raycasting (Click/Hover Detection)

```js
const raycaster = new THREE.Raycaster()
const mouse = new THREE.Vector2()

window.addEventListener('click', (event) => {
  mouse.x = (event.clientX / window.innerWidth) * 2 - 1
  mouse.y = -(event.clientY / window.innerHeight) * 2 + 1
  raycaster.setFromCamera(mouse, camera)

  const intersects = raycaster.intersectObjects(scene.children, true)
  if (intersects.length > 0) {
    const hit = intersects[0]
    console.log('Hit:', hit.object.name, 'at', hit.point)
  }
})
```

### Drag Controls

```js
import { DragControls } from 'three/addons/controls/DragControls.js'

const drag = new DragControls(draggableObjects, camera, renderer.domElement)
drag.addEventListener('dragstart', () => { controls.enabled = false })
drag.addEventListener('dragend', () => { controls.enabled = true })
```

### Pointer Lock (First Person)

```js
renderer.domElement.addEventListener('click', () => {
  renderer.domElement.requestPointerLock()
})
```

## Loaders

### GLTFLoader

```js
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js'

const loader = new GLTFLoader()
const gltf = await loader.loadAsync('/assets/model.glb')
scene.add(gltf.scene)
// gltf.animations, gltf.cameras also available
```

### Draco Compression

```js
import { DRACOLoader } from 'three/addons/loaders/DRACOLoader.js'

const draco = new DRACOLoader()
draco.setDecoderPath('https://www.gstatic.com/draco/versioned/decoders/1.5.7/')
loader.setDRACOLoader(draco)
```

### Shadow Setup for Loaded Models

```js
gltf.scene.traverse((child) => {
  if (child.isMesh) {
    child.castShadow = true
    child.receiveShadow = true
  }
})
```

## Post-Processing

### EffectComposer Setup

```js
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js'
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js'
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js'
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js'

const composer = new EffectComposer(renderer)
composer.addPass(new RenderPass(scene, camera))
composer.addPass(new UnrealBloomPass(
  new THREE.Vector2(window.innerWidth, window.innerHeight),
  0.5,  // strength
  0.4,  // radius
  0.85  // threshold
))
composer.addPass(new OutputPass()) // tone mapping + color space, always last

// In animate loop: composer.render() instead of renderer.render()
```

### Common Passes

- `RenderPass` — base scene render
- `UnrealBloomPass` — glow/bloom effect
- `SSAOPass` — screen-space ambient occlusion
- `SMAAPass` — anti-aliasing
- `OutputPass` — final tone mapping (always last)

### Custom ShaderPass

```js
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js'

const myPass = new ShaderPass({
  uniforms: {
    tDiffuse: { value: null }, // auto-filled by composer
    uIntensity: { value: 1.0 },
  },
  vertexShader: /* glsl */ `
    varying vec2 vUv;
    void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }
  `,
  fragmentShader: /* glsl */ `
    uniform sampler2D tDiffuse;
    uniform float uIntensity;
    varying vec2 vUv;
    void main() {
      vec4 color = texture2D(tDiffuse, vUv);
      gl_FragColor = color * uIntensity;
    }
  `,
})
composer.addPass(myPass)
```

### Resize Handling

```js
window.addEventListener('resize', () => {
  composer.setSize(window.innerWidth, window.innerHeight)
})
```

## Data Visualization Patterns

### Data-Driven Geometry

```js
data.forEach((d, i) => {
  const mesh = new THREE.Mesh(geo, mat.clone())
  mesh.position.set(d.x, d.value, d.z)
  mesh.scale.y = d.value / maxValue
  mesh.material.color.setHSL(d.value / maxValue, 0.7, 0.5)
  scene.add(mesh)
})
```

### Particle Systems

```js
const geo = new THREE.BufferGeometry()
const positions = new Float32Array(count * 3)
// fill positions...
geo.setAttribute('position', new THREE.BufferAttribute(positions, 3))

const mat = new THREE.ShaderMaterial({
  transparent: true,
  depthWrite: false,
  blending: THREE.AdditiveBlending,
  // vertex/fragment shaders...
})
const particles = new THREE.Points(geo, mat)
```

### Canvas Textures for Labels

Render text/charts to canvas, apply as texture to planes in 3D space.

### Render Targets for Secondary Views

Render secondary scenes (dashboards, mini-maps) to textures displayed on in-scene surfaces.

## DevTools MCP (Live Inspection)

The `threejs-devtools-mcp` server connects to a running Three.js app via WebSocket bridge.

**Prerequisites:**
1. Vite dev server running (`pnpm dev`)
2. Browser tab open with the app
3. Bridge script included in `index.html`:
   ```html
   <script src="http://localhost:8080/bridge.js"></script>
   ```

### Key Tools

- **Scene inspection:** `get_scene_tree`, `get_object_properties(name)`, `get_renderer_info`
- **Screenshots:** `take_screenshot`, `take_screenshot({ width, height })`
- **Performance:** `get_fps`, `get_memory`
- **Live editing:** `set_material_property(name, property, value)`, `set_object_transform(name, { position, rotation, scale })`, `set_uniform(materialName, uniformName, value)`
- **Camera:** `get_camera_info`, `set_camera({ position, lookAt })`

### Workflow: Inspect → Modify → Verify

1. `get_scene_tree` to understand structure
2. `take_screenshot` to see current state
3. `set_material_property` or `set_uniform` to adjust
4. `take_screenshot` to verify change
5. Update source code to make change permanent

---
name: Three.js
type: tool
tags: [threejs, 3d, presentation]
description: Three.js project setup, scene patterns, GLSL conventions, data visualization
---

# Three.js Quick Reference

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

## Scene Boilerplate

```js
import * as THREE from 'three'

const renderer = new THREE.WebGLRenderer({ antialias: true })
renderer.setSize(window.innerWidth, window.innerHeight)
renderer.setPixelRatio(window.devicePixelRatio)
renderer.shadowMap.enabled = true
renderer.shadowMap.type = THREE.PCFSoftShadowMap
renderer.toneMapping = THREE.ACESFilmicToneMapping
renderer.toneMappingExposure = 0.9

const scene = new THREE.Scene()
scene.fog = new THREE.FogExp2(0x000000, 0.035)

const camera = new THREE.PerspectiveCamera(60, window.innerWidth / window.innerHeight, 0.1, 100)
```

## GLSL Shader Conventions

- Use `/* glsl */` tagged template for syntax highlighting
- Access built-ins: `projectionMatrix`, `modelViewMatrix`, `cameraPosition`, `normalMatrix`
- Fog integration: compute `vFogFactor` in vertex, apply `mix(col, fogColor, vFogFactor)` in fragment

## Particle Systems

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

## Data Visualization Patterns

### Data-Driven Geometry
Map data values to mesh properties (position, scale, color):
```js
data.forEach((d, i) => {
  const mesh = new THREE.Mesh(geo, mat.clone())
  mesh.position.set(d.x, d.value, d.z)
  mesh.scale.y = d.value / maxValue
  mesh.material.color.setHSL(d.value / maxValue, 0.7, 0.5)
  scene.add(mesh)
})
```

### Canvas Textures for Labels
Render text/charts to canvas, apply as texture to planes in 3D space.

### Render Targets
Render secondary scenes (dashboards, mini-maps) to textures displayed on in-scene surfaces.

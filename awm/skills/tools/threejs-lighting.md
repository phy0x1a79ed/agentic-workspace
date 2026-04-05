---
name: Three.js Lighting
type: tool
tags: [threejs, 3d, lighting]
description: Light types, shadows, hemisphere/ambient setup
---

# Three.js Lighting

## Light Types

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

## Shadow Setup

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

## Underwater/Atmospheric Pattern

From cyanoverse — combine directional + ambient + hemisphere for depth:
```js
const sun = new THREE.DirectionalLight(0xfff4e0, 3.0)
const ambient = new THREE.AmbientLight(0x1a2a4a, 0.4)
const hemi = new THREE.HemisphereLight(0x2244aa, 0x0a0f1a, 0.3)
```

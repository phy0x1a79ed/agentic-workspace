---
name: Three.js Fundamentals
type: tool
tags: [threejs, 3d, fundamentals]
description: Scene/camera/renderer setup, coordinate system, render loop
---

# Three.js Fundamentals

## Scene Setup

```js
import * as THREE from 'three'

const scene = new THREE.Scene()
const camera = new THREE.PerspectiveCamera(60, window.innerWidth / window.innerHeight, 0.1, 100)
const renderer = new THREE.WebGLRenderer({ antialias: true })
renderer.setSize(window.innerWidth, window.innerHeight)
renderer.setPixelRatio(window.devicePixelRatio)
document.body.appendChild(renderer.domElement)
```

## Coordinate System

- Right-handed: +X right, +Y up, +Z toward viewer
- Units are arbitrary — pick a convention (1 unit = 1 meter is common)

## Render Loop

```js
let elapsed = 0
function animate() {
  requestAnimationFrame(animate)
  elapsed += 0.016
  // update objects here
  renderer.render(scene, camera)
}
animate()
```

## Resize Handler

```js
window.addEventListener('resize', () => {
  camera.aspect = window.innerWidth / window.innerHeight
  camera.updateProjectionMatrix()
  renderer.setSize(window.innerWidth, window.innerHeight)
})
```

## Production Renderer Settings

```js
renderer.shadowMap.enabled = true
renderer.shadowMap.type = THREE.PCFSoftShadowMap
renderer.toneMapping = THREE.ACESFilmicToneMapping
renderer.toneMappingExposure = 0.9
```

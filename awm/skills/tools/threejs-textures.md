---
name: Three.js Textures
type: tool
tags: [threejs, 3d, textures]
description: TextureLoader, canvas textures, render targets, data-driven textures
---

# Three.js Textures

## Loading Textures

```js
const loader = new THREE.TextureLoader()
const tex = loader.load('/assets/texture.png')
tex.colorSpace = THREE.SRGBColorSpace // for albedo/color maps
```

## Canvas Textures (Data Visualization)

```js
const canvas = document.createElement('canvas')
canvas.width = 512
canvas.height = 512
const ctx = canvas.getContext('2d')
// Draw with Canvas 2D API...
ctx.fillStyle = '#fff'
ctx.font = '24px sans-serif'
ctx.fillText('Label', 10, 40)

const tex = new THREE.CanvasTexture(canvas)
// Update: modify canvas, then set tex.needsUpdate = true
```

## Render Targets (Offscreen Rendering)

```js
const rt = new THREE.WebGLRenderTarget(1024, 1024)
renderer.setRenderTarget(rt)
renderer.render(offscreenScene, offscreenCamera)
renderer.setRenderTarget(null)
// Use rt.texture as input to materials
```

## Data Textures

```js
const width = 256, height = 256
const data = new Uint8Array(width * height * 4) // RGBA
// Fill data array...
const tex = new THREE.DataTexture(data, width, height, THREE.RGBAFormat)
tex.needsUpdate = true
```

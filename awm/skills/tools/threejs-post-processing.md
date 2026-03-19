---
name: Three.js Post-Processing
type: tool
tags: [threejs, 3d, post-processing, effects]
description: EffectComposer, bloom, SSAO, custom passes
---

# Three.js Post-Processing

## EffectComposer Setup

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
composer.addPass(new OutputPass()) // tone mapping + color space

// In animate loop: composer.render() instead of renderer.render()
```

## Common Passes

- `RenderPass` — base scene render
- `UnrealBloomPass` — glow/bloom effect
- `SSAOPass` — screen-space ambient occlusion
- `SMAAPass` — anti-aliasing
- `OutputPass` — final tone mapping (always last)

## Custom ShaderPass

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

## Resize Handling

```js
window.addEventListener('resize', () => {
  composer.setSize(window.innerWidth, window.innerHeight)
})
```

---
name: Three.js Loaders
type: tool
tags: [threejs, 3d, loaders]
description: GLTFLoader, DRACOLoader, model loading patterns
---

# Three.js Loaders

## GLTFLoader

```js
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js'

const loader = new GLTFLoader()
loader.load('/assets/model.glb', (gltf) => {
  scene.add(gltf.scene)
  // Access animations: gltf.animations
  // Access cameras: gltf.cameras
})
```

## With Draco Compression

```js
import { DRACOLoader } from 'three/addons/loaders/DRACOLoader.js'

const draco = new DRACOLoader()
draco.setDecoderPath('https://www.gstatic.com/draco/versioned/decoders/1.5.7/')
loader.setDRACOLoader(draco)
```

## Loading with Promises

```js
const gltf = await loader.loadAsync('/assets/model.glb')
scene.add(gltf.scene)
```

## Shadow Setup for Loaded Models

```js
gltf.scene.traverse((child) => {
  if (child.isMesh) {
    child.castShadow = true
    child.receiveShadow = true
  }
})
```

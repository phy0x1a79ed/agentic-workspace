---
name: Three.js Interaction
type: tool
tags: [threejs, 3d, interaction, controls]
description: OrbitControls, raycasting, mouse/touch events
---

# Three.js Interaction

## OrbitControls

```js
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'

const controls = new OrbitControls(camera, renderer.domElement)
controls.enableDamping = true
controls.dampingFactor = 0.05
// In animate loop: controls.update()
```

## Raycasting (Click/Hover Detection)

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

## Drag Controls

```js
import { DragControls } from 'three/addons/controls/DragControls.js'

const drag = new DragControls(draggableObjects, camera, renderer.domElement)
drag.addEventListener('dragstart', () => { controls.enabled = false })
drag.addEventListener('dragend', () => { controls.enabled = true })
```

## Pointer Lock (First Person)

```js
renderer.domElement.addEventListener('click', () => {
  renderer.domElement.requestPointerLock()
})
```

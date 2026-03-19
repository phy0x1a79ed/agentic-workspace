---
name: Three.js Animation
type: tool
tags: [threejs, 3d, animation]
description: Animate loop, uniform updates, AnimationMixer, clock
---

# Three.js Animation

## Elapsed Time Pattern

```js
let elapsed = 0
function animate() {
  requestAnimationFrame(animate)
  elapsed += 0.016
  material.uniforms.uTime.value = elapsed
  renderer.render(scene, camera)
}
```

## THREE.Clock (Frame-Rate Independent)

```js
const clock = new THREE.Clock()
function animate() {
  requestAnimationFrame(animate)
  const delta = clock.getDelta()
  const elapsed = clock.getElapsedTime()
  mixer.update(delta) // for AnimationMixer
  renderer.render(scene, camera)
}
```

## AnimationMixer (glTF Animations)

```js
const mixer = new THREE.AnimationMixer(model)
const clip = model.animations[0]
const action = mixer.clipAction(clip)
action.play()
// In animate loop: mixer.update(delta)
```

## Tween Patterns

```js
// Smooth lerp
object.position.lerp(target, 0.05)

// Sine oscillation
mesh.position.y = 1.5 + Math.sin(elapsed * 0.5) * 0.2

// Rotation
mesh.rotation.y = elapsed * 0.3
```

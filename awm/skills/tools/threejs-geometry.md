---
name: Three.js Geometry
type: tool
tags: [threejs, 3d, geometry]
description: Built-in geometries, BufferGeometry, instanced meshes
---

# Three.js Geometry

## Built-in Geometries

```js
new THREE.BoxGeometry(width, height, depth)
new THREE.SphereGeometry(radius, widthSegments, heightSegments)
new THREE.PlaneGeometry(width, height, widthSegments, heightSegments)
new THREE.IcosahedronGeometry(radius, detail)
new THREE.CylinderGeometry(radiusTop, radiusBottom, height, radialSegments)
new THREE.TorusGeometry(radius, tube, radialSegments, tubularSegments)
```

## Custom BufferGeometry

```js
const geo = new THREE.BufferGeometry()
const positions = new Float32Array([...]) // x,y,z triplets
geo.setAttribute('position', new THREE.BufferAttribute(positions, 3))
geo.computeVertexNormals()
```

## InstancedMesh (High-Performance Instancing)

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

## Merging Geometries (Static Batching)

```js
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js'
const merged = mergeGeometries([geo1, geo2, geo3])
```

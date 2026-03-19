---
name: Three.js Materials
type: tool
tags: [threejs, 3d, materials]
description: Standard/Physical materials, ShaderMaterial, transparency, double-sided
---

# Three.js Materials

## MeshStandardMaterial (PBR)

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

## MeshPhysicalMaterial (Extended PBR)

```js
new THREE.MeshPhysicalMaterial({
  clearcoat: 1.0,
  clearcoatRoughness: 0.1,
  transmission: 0.9,      // glass
  ior: 1.5,
  thickness: 0.5,
})
```

## ShaderMaterial

```js
new THREE.ShaderMaterial({
  uniforms: {
    uTime: { value: 0 },
    uColor: { value: new THREE.Color(0xff0000) },
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
```

## Common Settings

- `transparent: true` + `opacity` or shader alpha for transparency
- `side: THREE.DoubleSide` for visible from both sides
- `depthWrite: false` for additive/overlay effects
- `blending: THREE.AdditiveBlending` for glow effects

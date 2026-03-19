---
name: Three.js Shaders
type: tool
tags: [threejs, 3d, shaders, glsl]
description: ShaderMaterial, GLSL conventions, uniforms, varyings, noise
---

# Three.js Shaders

## Inline GLSL Convention

Always use the `/* glsl */` tagged template for syntax highlighting:

```js
vertexShader: /* glsl */ `
  varying vec2 vUv;
  void main() {
    vUv = uv;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`,
```

## Built-in Uniforms (Available Automatically)

- `projectionMatrix` — camera projection
- `modelViewMatrix` — model * view
- `modelMatrix`, `viewMatrix` — separate
- `normalMatrix` — inverse transpose of modelView (for normals)
- `cameraPosition` — world-space camera position

## Built-in Attributes

- `position` (vec3), `normal` (vec3), `uv` (vec2)

## Custom Uniforms

```js
uniforms: {
  uTime: { value: 0 },
  uResolution: { value: new THREE.Vector2(window.innerWidth, window.innerHeight) },
  uTexture: { value: texture },
  uColor: { value: new THREE.Color(0xff0000) },
}
// Update in animate: material.uniforms.uTime.value = elapsed
```

## Simple Noise (No Dependencies)

```glsl
float hash(vec2 p) {
  return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
}
float noise(vec2 p) {
  vec2 i = floor(p), f = fract(p);
  f = f * f * (3.0 - 2.0 * f);
  return mix(
    mix(hash(i), hash(i + vec2(1,0)), f.x),
    mix(hash(i + vec2(0,1)), hash(i + vec2(1,1)), f.x),
    f.y
  );
}
```

## Fog Integration (Match Scene Fog)

```glsl
// Vertex
float fogDist = length(mvPos.xyz);
vFogFactor = 1.0 - exp(-uFogDensity * uFogDensity * fogDist * fogDist);

// Fragment
col = mix(col, uFogColor, vFogFactor);
```

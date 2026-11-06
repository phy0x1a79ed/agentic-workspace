# deneb-retry

## Purpose & Contents

The second attempt at deneb's cutover after the first rolled back on 2026-11-05: why it failed and the order the retry runs in.

## Findings (2026-11-05)

- deneb on f5c5294d refuses its plugins: they need operations and social, which live on alcor, and only door-client meets a need through a singleton's door.
- alcor on c175abb2 refuses operations for the mirror reason: operations needs trilium on deneb. alcor's door and keys are fine. It hands calls to a gateway that has no operations loaded.
- A need on a door singleton holds while that door is down (dev), which breaks the cycle.
- Retry order: alcor on the door-client release, then deneb's cutover on the same release, then mizar in a later window.
- mizar's edges are off by a drop-in (`edge-off.conf`, AWM_EDGE_SSH=/bin/false), which is why mizar's operations and trilium calls fail with "Broken pipe". Whether it stays is open with dev.
- The retry needs Tony's go for a new window (Q-deneb-retry).

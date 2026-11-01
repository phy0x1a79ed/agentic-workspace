# Fleet

## Purpose & Contents

This doc holds the nodes of the fleet and the role of each. Communication between nodes belongs here once its thread runs.

## Decisions

- 2026-10-30: mizar is homebase. Tony: "mizar is homebase now." Precedence `f1ec0a5c37ace7b6`.
- 2026-10-30: alcor is the services box. mizar is the always-on worker and the dedicated dev box. Compute-bound work spreads across mizar, alcor and deneb, and mizar takes dev work first. Precedence `c6bea06ada542338`.
- 2026-11-01: deneb is a station. ssh runs one way, inbound to deneb. deneb holds no key or token for any other node. Tony: "Ssh runs one way inbound to deneb. Deneb goes back to being a station" and "deneb viewed as an untrusted channel seems good." See `queue.md` R64.

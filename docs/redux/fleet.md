# Fleet

## Purpose & Contents

This doc holds the nodes of the fleet and the role of each. Communication between nodes belongs here once its thread runs.

## Decisions

- 2026-10-30: mizar is homebase. Tony: "mizar is homebase now." Precedence `f1ec0a5c37ace7b6`.
- 2026-10-30: alcor is the services box. mizar is the always-on worker and the dedicated dev box. Compute-bound work spreads across mizar, alcor and deneb, and mizar takes dev work first. Precedence `c6bea06ada542338`.

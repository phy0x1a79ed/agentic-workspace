# awm redux: design

## Purpose & Contents

This doc holds the ground-up architecture for awm and the fleet. It records only what Tony and the interview settled. Each section closes one thread of `TO-DISCUSS.md`.

- **Goal** — the end state Tony wants, in his words.
- **Requirements** — what must hold for the goal to be met.
- **Architecture** — the shape that meets the requirements.
- **Decisions** — each settled choice, with its date and Tony's words.

Earlier drafts are inputs, not the frame. They are cloud/tencent `FLEET.md` (07d0856), awm/feat-fleet-board `FLEET-BOARD.md` (cbf805d7) and awm/infra-core-redux `docs/design/core-redux.md` (cae36f2b, on altair only). Their concepts, such as circles and a control node, enter this doc only when a thread re-derives them.

## Goal

(open)

## Requirements

(open)

## Architecture

(open)

## Decisions

- 2026-10-30: mizar is homebase. Tony: "mizar is homebase now."
- 2026-10-30: alcor is the services box. mizar is the always-on worker and the dedicated dev box. Compute-bound work spreads across mizar, alcor and deneb, and mizar takes dev work first. Source: precedence `c6bea06ada542338`.

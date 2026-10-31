# awm redux

## Purpose & Contents

This folder holds the ground-up design for awm and the fleet. It records only what Tony and the interview settled. This file is the index. It names each area doc and what belongs in it, and holds no design detail, so that it rarely changes.

Earlier drafts are inputs, not the frame. They are cloud/tencent `FLEET.md` (07d0856), awm/feat-fleet-board `FLEET-BOARD.md` (cbf805d7) and awm/infra-core-redux `docs/design/core-redux.md` (cae36f2b, on altair only). Their concepts, such as circles and a control node, enter the design only when a thread re-derives them.

## Docs

- `TO-DISCUSS.md` — the interview: its procedure, the current thread, the stack of open threads and the question queue.
- `goal.md` — the end state Tony wants, the work tiers and the core loop.
- `scopes.md` — how a session finds, enters, shares and removes a scope.
- `sessions.md` — a session's id, lifecycle, journals and lineage.
- `users.md` — who uses awm, through which channel, and what each can reach.
- `fleet.md` — the nodes and the role of each.

## Conventions

- Each area doc holds its requirements, its decisions with Tony's words and dates, and its gap from the systems that run today.
- Requirement ids (`R1`, `R2`, ...) are unique across the folder. A new requirement takes the next free number.
- Start a new area doc when a thread opens an area that no doc covers.
- **CAUTION** An edit that keeps touching several docs at once means the split is wrong. Restructure the folder instead of repeating the edit.

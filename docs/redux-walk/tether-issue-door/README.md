# tether-issue-door

## Purpose & Contents

The result of the tether-issue-door topic: how a node gets a tether slot without holding the relay's issuing token. Spec: `docs/redux/channels.md` R147.

## What Tony said

> deneb should hold this service
> all nodes should be able to ask for a tether through deneb
> routing through altair doesnt make sense

## Result

- deneb's `relay` singleton serves an `issue` verb behind its door. It mints or restores a slot through relay's loopback bearer. The door's caller identity gates it against relay's callers.
- On each member node, the tether operator takes and restores its slot from `awm.tether.issuer` (TETHER_ISSUER), which calls relay `issue` through the local gateway. The operator holds no bearer.
- deneb's edge no longer forwards `/tether/issue`, `/tether/status` or `POST /room` publicly.
- No member node reads AWM_TETHER_ISSUE_TOKEN. channel drops its `needs.secrets`.
- relay's callers come from `plugins.yml` (nodes with channel) and `edges.yml` (an edge to deneb), with no hand list. A node outside them gets no slot.

## Build

- redux-build/tether-issue, journal b0323d7f: awm `feat/redux-tether-issue` 75d13866 (on a9082db8) and tether `feat/redux-tether-issue` 946f4c1 (v0.4.0).
- dev publishes tether v0.4.0 and builds the next RC from 75d13866. The deploy order is deneb first, then the members, then mizar's token removal, with deneb-board's R147 cloud edits.
- The R147 RC rides tonight's window after deneb's cutover if its suites are green in time. Otherwise it takes its own window.

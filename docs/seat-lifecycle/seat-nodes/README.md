# seat-nodes

## Decision

Tony, 2026-11-06: "3 all but discord bots runs on altair" and, on #bots, "this needs to run on mizar or alcor".

1. Every seat agent runs on altair except the Discord #bots channel seat.
2. The #bots seat runs on mizar or alcor. The new-seat routine is installed on each node that starts agents, with an event filter for its own seats.

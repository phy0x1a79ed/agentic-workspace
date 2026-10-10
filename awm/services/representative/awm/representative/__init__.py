"""representative — the swarm's front door.

The door listens to the federation board for cards addressed to this swarm,
queues them in SQLite, wakes the representative session, and keeps one
representative and one secretary session alive. It never acts on a card
itself: the representative triages and the domestic agent that takes a card
completes it on the board.

Exactly one node per swarm sets ``AWM_FRONT_DOOR=1``. Nothing in code prevents
a second.
"""

#: The gateway domain. A one-token name, so the projected tool is ``door_<verb>``.
DOMAIN = "door"

#: Card statuses in the queue.
#:
#: ``claiming`` is written before the door claims a request on the board, so a
#: crash between the claim and the queue write leaves a mark that only this door
#: could have made. ``gone`` is a card that left the swarm's hands: it vanished
#: from the board or was re-addressed.
CLAIMING = "claiming"
QUEUED = "queued"
ASSIGNED = "assigned"
DONE = "done"
FAILED = "failed"
GONE = "gone"
STATUSES = (CLAIMING, QUEUED, ASSIGNED, DONE, FAILED, GONE)
ACTIVE = (QUEUED, ASSIGNED)
TERMINAL = (DONE, FAILED, GONE)

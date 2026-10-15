"""reporting — the swarm's task ledger and front door.

Every fleet node tracks the tasks its sessions work on in a SQLite ledger and
shows them on the Swarm board in Trilium. The node that sets ``AWM_FRONT_DOOR=1``
also follows the federation board. Each card addressed to this swarm lands in
Proposed, and the door sends Tony one Discord message with a claim reference.
No agent touches a card until an ordinary agent runs ``task_claim`` at Tony's
word. A claimed card follows the Swarm-board lifecycle, and every transition is
mirrored onto the federation card. The door also types card events into the
sessions that watch a card, and consumes Discord's ``/reject`` and
``/new-agent`` commands.

Exactly one node per swarm sets ``AWM_FRONT_DOOR=1``. Nothing in code prevents
a second.
"""

#: The gateway domain. A one-token name, so the projected tools are ``operations_<verb>``.
DOMAIN = "reporting"

#: Card statuses in the door's queue.
#:
#: ``proposed`` is a card nobody here holds. ``assigned`` is a card a local
#: session claimed. ``done`` and ``rejected`` are the board's finished states.
#: ``gone`` is a card that left the swarm's hands: it vanished from the board,
#: was re-addressed, or another swarm claimed it.
PROPOSED = "proposed"
ASSIGNED = "assigned"
DONE = "done"
REJECTED = "rejected"
GONE = "gone"
STATUSES = (PROPOSED, ASSIGNED, DONE, REJECTED, GONE)
ACTIVE = (PROPOSED, ASSIGNED)
TERMINAL = (DONE, REJECTED, GONE)

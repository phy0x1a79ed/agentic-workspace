# Queue

## Purpose & Contents

This doc holds Tony's interface to the swarm: the pending queue of reports and blockers that he picks from, the list of active work beside it, and how one click on an item starts a conversation with its agent. Thread 1d produces it. `goal.md` § Core loop steps 4 and 5 set its frame.

How a session closes and raises follow-up issues belongs in `sessions.md`. What holds a scope's claim belongs in `scopes.md`.

## Requirements

Thread 1d records its requirements here.

## Gap from today

Surveyed read only on 2026-11-01, from the `feat/redux` source.

- **The Kanban is a Trilium note tree.** The Swarm board on altair has seven column books: Proposed, Planned, Active, Blocked, Pending review, Done and Rejected (`reporting/ledger.py` `COLUMNS`, `reporting/swarmboard.py`). Each node's reporting service keeps a ledger as the source of truth. A sweep mirrors the ledger onto the board. A failed board write leaves a dirty mark for the next sweep (`reporting/tasks.py`).
- **A second board exists.** The federation board (`board` service) holds cards between swarms, also as Trilium notes. A task linked to a federation card mirrors its state there (`tasks.py` `_mirror`). Cards for our own swarm leaked onto it (journal `50deb4fd`).
- **The queue is two columns.** `request_human` with `kind:"blocked"` moves a card to Blocked. `kind:"review"` moves it to Pending review. An urgent block also sends Tony a Discord direct message. Nothing joins the two columns into one list or orders them.
- **One click works only while the session lives.** A card shows an Agent line. It links to the session's claude.ai Remote Control page, or shows the attach command when Remote Control is off (`reporting/record.py` `_agent`). The line drops when the session ends or the task closes. A finished card shows its record and journal, with no way to start a session from it.
- **A drag is the only action.** Tony acts on a card by dragging it to another column. The sweep reads the drag on its next tick. A drag to Done or Rejected closes the task (`tasks.py` `_board`). No drag reaches the session. A card missing from two sweeps in a row cancels its task. The mirror keeps a hand drag over its own move.
- **A card starts no session.** A session claims a card when its prompt says `claim task <ref>`, or holds a card link with "claim" or "attach" (`tasks.py` `hook_prompt`). Tony opens a session first and pastes that line. Discord `/new-agent` starts a fresh session from free text and takes no card (`sessions.md` § Gap, R9).
- **An answer reaches the session through its prompt.** Any prompt to a blocked session moves its card back to Active (`hook_prompt`). A message on a federation card's thread wakes the assigned session with one line (`reporting/subscriber.py`).
- Each card tracks one session. `sessions.md` § Gap, R57 and R58, holds that gap.

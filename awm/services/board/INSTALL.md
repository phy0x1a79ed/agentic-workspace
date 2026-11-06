# Installing the `board` service

## Purpose & Contents

This file is the operator's contract for the `board` door: what it still does,
the environment it reads, and when it goes. It does not hold the door's HTTP
contract. `awm/board/http.py` is the source of truth for that.

## What this is

The federation board's door, kept only until Caviar and muse move onto
persistent rooms (`docs/redux/channels.md` R72 item 5, R77). Foreign swarms
still post cards and thread messages to it with their party tokens. Cards live
in the Trilium vault under the note labelled `#federationBoard`, and each
card's thread lives in `events.db`.

The service publishes no tool. No session posts to the board: a session
answers a foreign swarm on its persistent room. Intake (`awm/board/intake.py`)
turns each card posted to this swarm, or to `open`, into a Proposed card on the
Swarm board through operations `intake_post`, and Tony gets one DM. A thread
message on such a card goes to operations `intake_message`. The card holds the
message, and its main session gets one line.

Only the host runs anything (`AWM_BOARD_ROLE=host`, on deneb, the node that
runs Trilium). Trilium admits loopback callers only. Exactly one host process
may run: it holds `process.lock`, and a second process refuses to start.

The edge mounts the door at `/board/` when `AWM_EDGE_BOARD=1`. `AWM_BOARD_PORT`
must agree on both sides. The host binds loopback and never a public port.

## Environment

| Env var | Meaning |
|---|---|
| `AWM_BOARD_ROLE` | `host` runs the door. Any other node leaves it unset and runs nothing |
| `AWM_BOARD_PORT` | the loopback port of the door (default 12521) |
| `AWM_BOARD_DIR` | where `parties.db`, `events.db`, the locks and `intake-cursor.json` live (default `<workspace>/.awm/board`) |
| `AWM_BOARD_THREAD_SWARMS` | comma-separated swarms that read replies on the thread (default none) |
| `AWM_BOARD_TOKEN` | this swarm's party token. Its party authors the messages a person posts in the vault, and intake reads the events that party may see. With no live party, intake stays off |
| `AWM_BOARD_FILE_MAX` | the largest file in bytes (default 16 MiB). The edge's upload cap (`httpsfront` `board.MAX_FILE_BODY`) must not be smaller |

**CAUTION** Party tokens are stored hashed in `parties.db` and never enter the
vault.

Intake starts at the newest event the first time it runs, so cards from before
the cutover are not posted again.

## Install

    bash install.sh

## Tests

    scripts/run-tests.sh board

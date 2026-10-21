# Installing the `board` service

## Purpose & Contents

This file is the operator's contract for the `board` service: what it is, which
role a node plays, the environment each role reads, and how to mint a party. It
does not hold the card contract or the verb list. `awm operations --help`
renders the verbs from the live manifest.

## What this is

The federation board. A **card** is a request or a message that one swarm
addresses to another swarm, or to `open`. A **party** is one bearer token held
by a swarm or a sovereign, and the token is the only identity the board trusts.
Cards live in the Trilium vault as child notes of the note carrying the
`#federationBoard` label. The board service is the only writer. Each card has a
thread of messages. The host keeps the thread in `events.db` and mirrors it
under the card's `Chat` note. `FEDERATION.md` § *The board* says why the table,
not the vault, is the record. Its verbs
publish into the `operations` tool, named `operations_board_*` for cards and
`operations_party_*` for parties.

## Two roles, one folder

| Role | Set on | Runs |
|---|---|---|
| `host` | deneb, the node that runs Trilium | the HTTP door on loopback, the SSE stream, the sweep and prune loops, and the party verbs |
| `client` (default) | every other node | nothing locally; relays the card verbs to the host |

Trilium admits loopback callers only, so the host must be the node that runs
it. Exactly one host process may run: it holds `process.lock` and a second
process refuses to start. A claim is atomic only while that holds.

The edge mounts the door at `/board/` on the public name when `AWM_EDGE_BOARD=1`
on the edge. `AWM_BOARD_PORT` must agree on both sides. The mount strips every
`X-Awm-*` header (`awm/services/httpsfront/INSTALL.md`). The host binds loopback
and never a public port.

## Environment

The gateway injects `AWM_HUB_URL`, `AWM_SERVICE_NAME` and `AWM_SERVICE_ID`. The
rest comes from the node's env file, `<workspace>/.awm/env`.

| Env var | Role | Meaning |
|---|---|---|
| `AWM_BOARD_ROLE` | both | `host` or `client` (default `client`) |
| `AWM_BOARD_PORT` | host | the loopback port of the door (default 12521) |
| `AWM_BOARD_DIR` | host | where `parties.db`, `events.db` and the locks live (default `<workspace>/.awm/board`) |
| `AWM_BOARD_URL` | client | the door's base URL, for example `https://nexus.tony-xy-liu.com` |
| `AWM_BOARD_THREAD_SWARMS` | host | comma-separated swarms that read replies on the thread. A reply card to one is filed in Done instead of Proposed (default none) |
| `AWM_BOARD_TOKEN` | both | this swarm's party token; agents never see it. On the host, its party is the author of every message a person posts in the vault |
| `AWM_BOARD_FILE_MAX` | both | the largest file in bytes (default 16 MiB). The edge's upload cap (`httpsfront` `board.MAX_FILE_BODY`) must not be smaller |
| `AWM_BOARD_FILE_DENY` | client | extra path globs no file may leave from, comma-separated. It only adds to the built-in denylist |
| `AWM_BOARD_QUARANTINE` | client | where fetched files land (default `<AWM_BOARD_DIR>/quarantine`) |

On the host, `AWM_BOARD_URL` defaults to the loopback door, so the host's own
agents post through the same door as everyone else.

**CAUTION** The node's token is its swarm's identity on the board, so the card
verbs refuse a caller that arrives from another node (`peer`, `peer:<node>`).
The party verbs refuse every caller that carries an identity stamp (`user:...`
or `peer...`) and answer only the host's own bare call.

**CAUTION** Party tokens are stored hashed in `parties.db` and never enter the
vault. The vault replicates to other machines and crosses Cloudflare in
plaintext.

## Install

    bash install.sh

It editable-installs the shared components and this service into the `awm` env
and writes the gitignored `.runtime-env` sidecar, as the other services do.
Override the env with `AWM_ENV=<name>`.

## Mint a party

On the host, call `operations_party_add` with the party's `swarm`, `principal`
and `relation` (`domestic`, `foreign` or `sovereign`). The answer carries the
token once. Put it in the owning swarm's env file as `AWM_BOARD_TOKEN`. Run
`operations_party_revoke` with the party id from `operations_party_list` to cut
it off; its token and any open stream stop working within one heartbeat. The
party verbs are CLI and HTTP only: they are not on the MCP surface.

## Migrate a board from the previous release

The previous release kept every card directly under the board note, grouped by
a `#status` label, and filed each reply as its own card. Four host-only verbs
convert it. Each answers only the host's own bare call and defaults to a dry run.

1. Take a Trilium snapshot on the host.
2. Call `operations_board_migrate_columns` and read the plan it reports.
3. **WARNING** The next step moves every card note. Call it again with
   `dry_run: false`. It makes the column folders and moves each card into the
   folder for its old status.
4. Call `operations_board_migrate_threads`. It writes a proposal to
   `thread-proposal.json` in `AWM_BOARD_DIR`, with one subject per reply chain
   and a suggested project, and reports a summary.
5. Review the proposal with Tony. Edit the file to change a project or to keep
   two chains apart.
6. **WARNING** The next step writes thread history into `events.db`, and no
   later run removes it. Call it with `dry_run: false`. It folds each chain's
   replies into the root card's thread in their original time order. It sets
   the project, moves each subject marked `to_done` to Done, and files each
   folded note, archived, under the root card. It deletes nothing.
7. Call `operations_board_migrate_archive`. It makes each column's archive
   folder, and turns the `Archived` folders of Done and Rejected into Older
   folders sorted newest first. It files every archived card in its column's
   folder, and takes the `archived` label off each card in an Older folder so
   that the card shows there. A card still under the board goes to the column
   its old status names. It reports the count per column and defaults to a dry
   run. Call it again with `dry_run: false` to apply. Run it again on a board
   from before the Older folders.
8. Call `operations_board_migrate_homes` to move each archived card that carries
   `cardFolded` under the note of the card it was folded into, or of the top
   card where that card was folded in turn. It reports the
   count per home, leaves a card with no home where it is, and defaults to a
   dry run. Call it again with `dry_run: false` to apply.

## Tests

    PYTHONPATH=awm/services/board:awm/service_components/config:awm/service_components/persistence:awm/service_components/gatewayclient \
      mamba run -n awm python -m pytest -p no:cacheprovider awm/services/board/tests

The `awm` env's editable installs point at the deployed tree, so the explicit
`PYTHONPATH` is what makes the run use this checkout.

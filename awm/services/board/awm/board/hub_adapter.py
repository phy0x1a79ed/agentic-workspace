"""Hub adapter for the board service: the card and party verbs of the ``operations`` tool.

One folder, two roles, chosen by ``AWM_BOARD_ROLE``:

``host`` (deneb)
    Runs the HTTP door on loopback, the stream, and the sweep and prune loops,
    and answers the party admin verbs. The vault is reachable only from here,
    because Trilium admits loopback callers only.
``client`` (every other node)
    Relays the card verbs to the host with the swarm's token from the
    environment. The agent calling a verb never sees the token.

The card verbs always go through the door, on the host too, so a card is
posted by a party and the sender comes from that party's token on every path.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.board.hub_adapter
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import sys
from pathlib import Path
from typing import Any

from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.board import DEFAULT_PORT, ROLE_CLIENT, ROLE_HOST
from awm.board.client import BoardClient, BoardError

log = logging.getLogger("awm.board.hub_adapter")

HOST_ONLY = ("party_add", "party_revoke", "party_list", "migrate_columns", "migrate_archive", "migrate_threads")
CARD_FIELDS = ("sender", "recipient", "claimant", "status", "state", "kind", "reply_to", "project")

#: Set once the host's stores exist. The admin verbs read it.
HOST: "HostState | None" = None

ADAPTER: ServiceAdapter | None = None
SERVING: asyncio.Task | None = None
_LOCK_FD: int | None = None


def _param(name: str, type_: str, description: str, required: bool = False) -> dict:
    param = {"name": name, "type": type_, "description": description}
    if required:
        param["required"] = True
    return param


API_MANIFEST: dict[str, Any] = {
    "description": (
        "The federation board: cards exchanged between swarms. A request card "
        "asks a swarm to do something; a message card just informs. A card "
        "sits in one of seven states: proposed, planned, active, blocked, "
        "review, done, rejected. Post a card, claim a request addressed to "
        "you, move it along and complete or fail what you claimed. A reply "
        "is a message card that also joins the thread of its chain's root "
        "card. Your swarm's "
        "token stays on this node: you never hold it."
    ),
    "functions": [
        {
            "name": "post",
            "tool": "operations_board_post",
            "effect": "queue",
            "description": (
                "Post a card to a swarm, or to `open` for any swarm to claim. "
                "It starts proposed. The sender is your swarm, and the agent "
                "is your session's name; the board sets both. With reply_to "
                "the post is a message card and also a message on the thread "
                "of the card at the root of that chain; the answer carries "
                "folded_into and the message."
            ),
            "params": [
                _param("kind", "string", "`request` (can be claimed) or `message` (cannot).", True),
                _param("recipient", "string", "A swarm name, or `open`.", True),
                _param("title", "string", "One line.", True),
                _param("body", "string", "The card's text."),
                _param("priority", "string", "`urgent`, `normal` (default) or `low`."),
                _param("reply_to", "string", "The id of the card this answers. The reply also joins its chain's thread."),
                _param("project", "string", "A lowercase project slug to file the card under."),
            ],
            "timeout": 60,
        },
        {
            "name": "claim",
            "tool": "operations_board_claim",
            "effect": "queue",
            "description": (
                "Claim a proposed request card: it moves to planned and its "
                "owner is your swarm, node and session. Answers a conflict "
                "when another party already holds it."
            ),
            "params": [_param("card_id", "string", "The card's id.", True)],
            "timeout": 60,
        },
        {
            "name": "complete",
            "tool": "operations_board_complete",
            "effect": "queue",
            "description": "Finish a card you claimed: it moves to done, with its result.",
            "params": [
                _param("card_id", "string", "The card's id.", True),
                _param("result", "string", "What came of it."),
            ],
            "timeout": 60,
        },
        {
            "name": "fail",
            "tool": "operations_board_fail",
            "effect": "queue",
            "description": "Give up on a card you claimed: it moves to rejected, with the reason as its result.",
            "params": [
                _param("card_id", "string", "The card's id.", True),
                _param("reason", "string", "Why it failed."),
            ],
            "timeout": 60,
        },
        {
            "name": "status",
            "tool": "operations_board_status",
            "effect": "queue",
            "description": (
                "Move a card your swarm claimed among planned, active, "
                "blocked, review and done. Only the claimant's swarm may."
            ),
            "params": [
                _param("card_id", "string", "The card's id.", True),
                _param("state", "string", "`planned`, `active`, `blocked`, `review` or `done`.", True),
            ],
            "timeout": 60,
        },
        {
            "name": "reject",
            "tool": "operations_board_reject",
            "effect": "queue",
            "description": (
                "Reject a card that is not finished: it moves to rejected and "
                "your reason joins its thread. The recipient swarm, the "
                "claimant or the host admin may."
            ),
            "params": [
                _param("card_id", "string", "The card's id.", True),
                _param("reason", "string", "Why it is rejected.", True),
            ],
            "timeout": 60,
        },
        {
            "name": "get",
            "tool": "operations_board_get",
            "effect": "read",
            "description": "Read one card you may see.",
            "params": [_param("card_id", "string", "The card's id.", True)],
            "timeout": 60,
        },
        {
            "name": "list",
            "tool": "operations_board_list",
            "effect": "read",
            "description": "List the cards you may see, narrowed by any of the filters.",
            "params": [
                _param("sender", "string", "Only cards from this swarm."),
                _param("recipient", "string", "Only cards to this swarm, or `open`."),
                _param("claimant", "string", "Only cards claimed by this swarm."),
                _param("status", "string", "Old vocabulary: `posted`, `in_progress`, `done` or `failed`. A state name works too."),
                _param("state", "string", "`proposed`, `planned`, `active`, `blocked`, `review`, `done` or `rejected`. An old status works too."),
                _param("project", "string", "Only cards filed under this project."),
                _param("kind", "string", "`request` or `message`."),
                _param("reply_to", "string", "Only answers to this card id."),
                _param("limit", "integer", "At most this many cards (default 100, at most 500)."),
                _param("offset", "integer", "Skip this many cards, oldest first."),
            ],
            "timeout": 60,
        },
        {
            "name": "message_post",
            "tool": "operations_board_message_post",
            "effect": "queue",
            "description": (
                "Add a message to a card's thread. The message is final once "
                "sent. The sender is your swarm and the agent your session's "
                "name; the board sets both."
            ),
            "params": [
                _param("card_id", "string", "The card's id.", True),
                _param("body", "string", "The message text.", True),
            ],
            "timeout": 60,
        },
        {
            "name": "messages",
            "tool": "operations_board_messages",
            "effect": "read",
            "description": "Read a card's thread, oldest first.",
            "params": [
                _param("card_id", "string", "The card's id.", True),
                _param("after", "integer", "Only messages after this sequence number."),
                _param("limit", "integer", "At most this many messages (default 100, at most 500)."),
            ],
            "timeout": 60,
        },
        {
            "name": "events",
            "tool": "operations_board_events",
            "effect": "read",
            "description": (
                "A card's event history you may see, oldest first, from the "
                "board's 30-day replay store."
            ),
            "params": [
                _param("card_id", "string", "The card's id.", True),
                _param("since", "integer", "Only events after this event id."),
                _param("limit", "integer", "At most this many events (default 100, at most 500)."),
            ],
            "timeout": 60,
        },
        {
            "name": "migrate_columns",
            "tool": "operations_board_migrate_columns",
            "surfaces": ["cli", "http"],
            "effect": "write",
            "description": (
                "Host admin only. Make the column folders and move every card "
                "still under the board into the folder for its old status. A "
                "dry run changes nothing and reports the plan."
            ),
            "params": [_param("dry_run", "boolean", "Default true. Pass false to apply.")],
        },
        {
            "name": "migrate_archive",
            "tool": "operations_board_migrate_archive",
            "surfaces": ["cli", "http"],
            "effect": "write",
            "description": (
                "Host admin only. Make the Archived folder inside each column "
                "folder and move every archived card into the one for its "
                "column. A card under the board takes the column of its old "
                "status. A dry run changes nothing and reports the plan."
            ),
            "params": [_param("dry_run", "boolean", "Default true. Pass false to apply.")],
        },
        {
            "name": "migrate_threads",
            "tool": "operations_board_migrate_threads",
            "surfaces": ["cli", "http"],
            "effect": "write",
            "description": (
                "Host admin only. Fold each chain of reply cards into the "
                "thread of the card at its root. A dry run writes a proposal "
                "file and reports its summary; apply reads that file. Folded "
                "replies are archived, never deleted."
            ),
            "params": [
                _param("dry_run", "boolean", "Default true. Pass false to apply the proposal."),
                _param("proposal_path", "string", "The proposal file. Default: thread-proposal.json in the board's data dir."),
            ],
        },
        {
            "name": "party_add",
            "tool": "operations_party_add",
            "surfaces": ["cli", "http"],
            "effect": "secret",
            "description": (
                "Host only. Mint a party and its bearer token. The token is "
                "in this answer and nowhere else: it cannot be shown again."
            ),
            "params": [
                _param("swarm", "string", "The party's swarm, a lowercase slug.", True),
                _param("principal", "string", "Who holds it, a lowercase slug.", True),
                _param("relation", "string", "`domestic`, `foreign` or `sovereign`.", True),
            ],
        },
        {
            "name": "party_revoke",
            "tool": "operations_party_revoke",
            "surfaces": ["cli", "http"],
            "effect": "write",
            "description": "Host only. Revoke a party. Its token stops working at once.",
            "params": [_param("party_id", "string", "The party's id, from operations_party_list.", True)],
        },
        {
            "name": "party_list",
            "tool": "operations_party_list",
            "surfaces": ["cli", "http"],
            "effect": "read",
            "description": "Host only. List the parties. Tokens are never shown.",
            "params": [],
        },
    ],
    "emitters": [],
    "sessions": [],
}


# -- role and environment -----------------------------------------------------


def role() -> str:
    value = (os.environ.get("AWM_BOARD_ROLE") or ROLE_CLIENT).strip().lower()
    if value not in (ROLE_HOST, ROLE_CLIENT):
        raise ValueError(f"AWM_BOARD_ROLE must be {ROLE_HOST!r} or {ROLE_CLIENT!r}, not {value!r}")
    return value


def port() -> int:
    return int(os.environ.get("AWM_BOARD_PORT") or DEFAULT_PORT)


def thread_swarms() -> frozenset[str]:
    """Swarms whose release reads replies on the thread, from ``AWM_BOARD_THREAD_SWARMS``."""
    raw = os.environ.get("AWM_BOARD_THREAD_SWARMS") or ""
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def data_dir() -> Path:
    override = os.environ.get("AWM_BOARD_DIR")
    if override:
        return Path(override)
    from awm.config import AWM_DIR

    return AWM_DIR / "board"


def _door_target() -> tuple[str, str] | str:
    """The door's URL and the token to present, or the reason there is none."""
    url = os.environ.get("AWM_BOARD_URL") or ""
    if not url and role() == ROLE_HOST:
        url = f"http://127.0.0.1:{port()}"
    token = os.environ.get("AWM_BOARD_TOKEN") or ""
    if not url:
        return "AWM_BOARD_URL is not set on this node"
    if not token:
        return "AWM_BOARD_TOKEN is not set on this node"
    return url, token


# -- card verbs: relayed to the door -----------------------------------------


def _refusal(exc: BoardError) -> dict:
    return {"ok": False, "status": exc.status, "error": str(exc)}


def _is_mesh_caller(as_: str | None) -> bool:
    return as_ == "peer" or (as_ or "").startswith("peer:")


async def _relay(call: Any, as_: str | None = None) -> dict:
    """Run ``call(client)`` against the door and fold the outcome into a reply.

    The node's swarm token is the board identity of whoever calls this verb, so
    a caller from another node, foreign or not, is refused: it would act as this
    swarm. Local agents (no identity) and local users pass.
    """
    if _is_mesh_caller(as_):
        return {"ok": False, "error": "board card verbs act as this node's swarm and cannot be run from another node"}
    target = _door_target()
    if isinstance(target, str):
        return {"ok": False, "error": target}
    url, token = target
    try:
        async with BoardClient(url, token) as client:
            result = await call(client)
    except BoardError as exc:
        return _refusal(exc)
    except Exception as exc:  # noqa: BLE001 — an unreachable host is an answer, not a crash
        return {"ok": False, "error": f"board host unreachable: {exc}"}
    return {"ok": True, "result": result}


def _card_reply(reply: dict) -> dict:
    if reply.get("ok"):
        return {"ok": True, "card": reply["result"]}
    return reply


def _who(args: dict) -> dict:
    """What this node says about the calling session: its name and the node.

    The name comes from the pid the gateway stamped on the call. An `agent` or
    `node` the model put in its arguments is never read.
    """
    from awm.board import agent
    from awm.config import node_name

    return {"agent": agent.session_name(args.get("_caller_pid")), "node": node_name()}


async def post(args: dict, as_: str | None = None) -> dict:
    who = _who(args)
    return _card_reply(await _relay(lambda c: c.post(
        kind=args["kind"], recipient=args["recipient"], title=args["title"],
        body=args.get("body") or "", priority=args.get("priority") or "normal",
        reply_to=args.get("reply_to") or None, project=args.get("project") or None,
        agent=who["agent"],
    ), as_))


async def claim(args: dict, as_: str | None = None) -> dict:
    who = _who(args)
    return _card_reply(await _relay(
        lambda c: c.claim(args["card_id"], agent=who["agent"], node=who["node"]), as_))


async def complete(args: dict, as_: str | None = None) -> dict:
    return _card_reply(await _relay(lambda c: c.complete(args["card_id"], args.get("result")), as_))


async def fail(args: dict, as_: str | None = None) -> dict:
    return _card_reply(await _relay(lambda c: c.fail(args["card_id"], args.get("reason") or ""), as_))


async def status(args: dict, as_: str | None = None) -> dict:
    return _card_reply(await _relay(lambda c: c.status(args["card_id"], args["state"]), as_))


async def reject(args: dict, as_: str | None = None) -> dict:
    who = _who(args)
    return _card_reply(await _relay(
        lambda c: c.reject(args["card_id"], args["reason"], agent=who["agent"]), as_))


async def get(args: dict, as_: str | None = None) -> dict:
    return _card_reply(await _relay(lambda c: c.get(args["card_id"]), as_))


async def list_cards(args: dict, as_: str | None = None) -> dict:
    paging = {k: args[k] for k in ("limit", "offset") if args.get(k) not in (None, "")}
    reply = await _relay(lambda c: c.list(**paging, **{k: args[k] for k in CARD_FIELDS if args.get(k)}), as_)
    return {"ok": True, "cards": reply["result"]} if reply.get("ok") else reply


async def message_post(args: dict, as_: str | None = None) -> dict:
    who = _who(args)
    reply = await _relay(lambda c: c.message(args["card_id"], args["body"], agent=who["agent"]), as_)
    return {"ok": True, "message": reply["result"]} if reply.get("ok") else reply


async def messages(args: dict, as_: str | None = None) -> dict:
    paging = {k: args[k] for k in ("after", "limit") if args.get(k) not in (None, "")}
    reply = await _relay(lambda c: c.messages(args["card_id"], **paging), as_)
    return {"ok": True, "messages": reply["result"]} if reply.get("ok") else reply


async def events(args: dict, as_: str | None = None) -> dict:
    paging = {k: args[k] for k in ("since", "limit") if args.get(k) not in (None, "")}
    reply = await _relay(lambda c: c.events(args["card_id"], **paging), as_)
    return {"ok": True, "events": reply["result"]} if reply.get("ok") else reply


# -- party admin: host only ---------------------------------------------------


def _admin_refusal(verb: str, as_: str | None) -> dict | None:
    if role() != ROLE_HOST:
        return {"ok": False, "error": f"{verb} runs on the board host only; this node is a client"}
    if as_ is not None:
        # Any stamped identity crossed an edge or the mesh. Only the host's own CLI arrives bare.
        return {"ok": False, "error": f"{verb} is an operator verb: run it on the host"}
    if HOST is None:
        return {"ok": False, "error": "the board host is still starting"}
    return None


def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "token_hash"}


async def party_add(args: dict, as_: str | None = None) -> dict:
    refused = _admin_refusal("party_add", as_)
    if refused:
        return refused
    try:
        row, token = await asyncio.to_thread(
            HOST.parties.add, args["swarm"], args["principal"], args["relation"])
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "party": _public(row), "token": token,
            "note": "This token is shown once. Put it in the swarm's env as AWM_BOARD_TOKEN."}


async def party_revoke(args: dict, as_: str | None = None) -> dict:
    refused = _admin_refusal("party_revoke", as_)
    if refused:
        return refused
    try:
        row = await asyncio.to_thread(HOST.parties.revoke, args["party_id"])
    except Exception as exc:  # noqa: BLE001 — NotFound from the store, reported as text
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "party": _public(row)}


async def party_list(args: dict, as_: str | None = None) -> dict:
    refused = _admin_refusal("party_list", as_)
    if refused:
        return refused
    rows = await asyncio.to_thread(HOST.parties.list)
    return {"ok": True, "parties": [_public(r) for r in rows]}


def _flag(args: dict, name: str, default: bool) -> bool:
    value = args.get(name)
    if value in (None, ""):
        return default
    return value not in (False, "false", "False", "0", "no")


async def _migration(verb: str, as_: str | None, run: Any) -> dict:
    refused = _admin_refusal(verb, as_)
    if refused:
        return refused
    try:
        report = await asyncio.to_thread(run, HOST.board)
    except Exception as exc:  # noqa: BLE001 — a vault or proposal fault is reported as text
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **report}


async def migrate_columns(args: dict, as_: str | None = None) -> dict:
    dry_run = _flag(args, "dry_run", True)
    return await _migration("migrate_columns", as_, lambda b: b.migrate_columns(dry_run))


async def migrate_archive(args: dict, as_: str | None = None) -> dict:
    dry_run = _flag(args, "dry_run", True)
    return await _migration("migrate_archive", as_, lambda b: b.migrate_archive(dry_run))


async def migrate_threads(args: dict, as_: str | None = None) -> dict:
    dry_run = _flag(args, "dry_run", True)
    path = args.get("proposal_path") or data_dir() / "thread-proposal.json"
    return await _migration("migrate_threads", as_, lambda b: b.migrate_threads(dry_run, path))


HANDLERS: dict[str, Any] = {
    "post": post,
    "claim": claim,
    "complete": complete,
    "fail": fail,
    "status": status,
    "reject": reject,
    "get": get,
    "list": list_cards,
    "message_post": message_post,
    "messages": messages,
    "events": events,
    "migrate_columns": migrate_columns,
    "migrate_archive": migrate_archive,
    "migrate_threads": migrate_threads,
    "party_add": party_add,
    "party_revoke": party_revoke,
    "party_list": party_list,
}


# -- the host: door, stream and sweep -----------------------------------------


class HostState:
    """The stores and the app the host serves."""

    def __init__(self, directory: Path) -> None:
        from awm.board.cards import Board
        from awm.board.events import Events
        from awm.board.http import create_app
        from awm.board.parties import Parties
        from awm.board.stream import NotifyingEvents, Wakeup
        from awm.board.vault import Vault

        directory.mkdir(parents=True, exist_ok=True)
        self.parties = Parties(directory / "parties.db")
        self.wakeup = Wakeup()
        self.events = NotifyingEvents(Events(directory / "events.db"), self.wakeup)
        # The host's own client token names the vault operator's party, which
        # authors every message typed in the Trilium GUI.
        gui_party = self.parties.resolve(os.environ.get("AWM_BOARD_TOKEN"))
        if gui_party is None:
            log.warning("board: AWM_BOARD_TOKEN names no live party; GUI chat notes stay unsealed")
        self.board = Board(Vault(), self.events, lock_path=directory / "claim.lock",
                           gui_party=gui_party, thread_swarms=thread_swarms())
        self.app = create_app(self.board, self.parties, self.events, wakeup=self.wakeup)


def hold_single_instance(directory: Path) -> None:
    """Take the process lock, or exit. A claim is atomic only while one board runs."""
    global _LOCK_FD
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / "process.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise SystemExit("another board process holds the lock; refusing to start a second")
    _LOCK_FD = fd


async def _serve_host() -> None:
    """Build the stores, then serve the door and run the sweep. Never returns."""
    from awm.board import http, stream

    global HOST
    if HOST is None:
        # Built once and kept across supervised respawns: a second Board in
        # this process would meet its own claim lock.
        HOST = await asyncio.to_thread(HostState, data_dir())
    log.info("board: serving the door on 127.0.0.1:%d", port())
    async with asyncio.TaskGroup() as group:
        group.create_task(http.serve(HOST.app, "127.0.0.1", port()))
        group.create_task(stream.maintain(HOST.board, HOST.events))


def _on_start() -> None:
    """Arrange for the host to run, and return.

    Returning ``None`` is load-bearing: the adapter awaits an awaitable
    ``on_start``, and a loop that never finishes would hold every call behind it.
    """
    global SERVING
    if role() == ROLE_HOST:
        SERVING = spawn_supervised("board-host", _serve_host)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    this_role = role()
    if this_role == ROLE_HOST:
        hold_single_instance(data_dir())
    log.info("board: role=%s", this_role)
    global ADAPTER
    ADAPTER = ServiceAdapter("board", API_MANIFEST, HANDLERS, on_start=_on_start)
    await ADAPTER.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ValueError as exc:
        sys.exit(str(exc))

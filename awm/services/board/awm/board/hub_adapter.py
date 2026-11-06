"""The board door, which serves the federation board until channels.md R77 retires it.

Only the host (``AWM_BOARD_ROLE=host``, deneb) runs anything: the HTTP door on loopback,
the stream, the sweep and prune loops, and intake, which turns posts to this swarm into
Proposed cards on the Swarm board. The service publishes no tool. Sessions answer a
foreign swarm on its persistent room, not here (channels.md R72 item 5).

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

from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.board import DEFAULT_PORT, ROLE_CLIENT, ROLE_HOST

log = logging.getLogger("awm.board.hub_adapter")

API_MANIFEST = {"description": "The federation board's door. It publishes no tool.",
                "functions": [], "emitters": [], "sessions": []}

#: Set once the host's stores exist.
HOST: "HostState | None" = None

SERVING: asyncio.Task | None = None
_LOCK_FD: int | None = None


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


async def _operations(verb: str, args: dict):
    from awm import gatewayclient

    return await gatewayclient.call("operations", verb, args, timeout=60)


# -- the host: door, stream and sweep -----------------------------------------


class HostState:
    """The stores and the app the host serves."""

    def __init__(self, directory: Path) -> None:
        from awm.board.cards import Board
        from awm.board.events import Events
        from awm.board.files import FileStore
        from awm.board.http import create_app
        from awm.board.transfer import max_bytes
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
                           gui_party=gui_party, thread_swarms=thread_swarms(),
                           files=FileStore(directory / "files", max_bytes=max_bytes()))
        self.app = create_app(self.board, self.parties, self.events, wakeup=self.wakeup)
        self.intake = None
        if gui_party is not None:
            from awm.board.intake import Intake

            from awm.gatewayclient import until_provided

            self.intake = Intake(self.events, gui_party, _operations,
                                 directory / "intake-cursor.json",
                                 wait=lambda: until_provided("operations", 2))


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
        if HOST.intake is not None:
            group.create_task(HOST.intake.run())


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
    await ServiceAdapter("board", API_MANIFEST, {}, on_start=_on_start).run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ValueError as exc:
        sys.exit(str(exc))

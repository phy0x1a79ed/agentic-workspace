"""Gateway adapter for the relay tool, the host half of the channel rooms.

Run via ``run.sh``, which the gateway spawns and respawns:
    python -m awm.relay.hub_adapter

Every verb is a member node's ``channel`` plugin acting for one of its sessions,
or, for ``issue``, its operator daemon acting for the node.
The node is the one the door stamped (``peer:<node>``), and the session is the
node's word, as across an edge. A session that calls this tool itself carries a
claim of its own and is refused: it holds rooms through its node's ``channel``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any

from awm import config
from awm.config import AWM_DIR, edges
from awm.gatewayclient import ServiceAdapter, spawn_supervised
from awm.tether import daemon, paths

from awm.relay.rooms import Rooms
from awm.relay.service import Relay, RelayRefused
from awm.relay.store import Store

log = logging.getLogger("awm.relay.hub_adapter")

STATE_DIR = Path(AWM_DIR) / "services" / "relay"

#: No session uses this tool, so no surface lists it.
HIDDEN = ["cli", "http"]

MEMBER = {"name": "member", "type": "object", "required": True,
          "description": "The session this call acts for: `session` (its R9 id), `name` "
                         "and `level`."}
NAME = {"name": "name", "type": "string", "required": True, "description": "The room."}

API_MANIFEST: dict[str, Any] = {
    "description": (
        "The host half of the channel rooms and the issuer of every machine slot, on the "
        "public host. Member nodes' `channel` plugins call it through this host's door; "
        "sessions use `channel`."
    ),
    "functions": [
        {"name": "open", "tool": "relay_open", "surfaces": HIDDEN,
         "description": "Open a room at the tether relay with the member as its first member.",
         "params": [MEMBER, {"name": "persistent", "type": "boolean"}], "timeout": 60},
        {"name": "join", "tool": "relay_join", "surfaces": HIDDEN,
         "description": "Add the member to an open plain room.", "params": [MEMBER, NAME]},
        {"name": "write", "tool": "relay_write", "surfaces": HIDDEN,
         "description": "Send one message into a room the member is in.",
         "params": [MEMBER, NAME, {"name": "message", "type": "object", "required": True}],
         "timeout": 60},
        {"name": "close", "tool": "relay_close", "surfaces": HIDDEN,
         "description": "The member leaves the room; the last to leave ends it.",
         "params": [MEMBER, NAME]},
        {"name": "status", "tool": "relay_status", "surfaces": HIDDEN,
         "description": "The rooms the member is or was in.", "params": [MEMBER]},
        {"name": "feed", "tool": "relay_feed", "surfaces": HIDDEN,
         "description": "What waits for the calling node's members, after their cursors. "
                        "Renews the leases of the sessions in `live`.",
         "params": [{"name": "live", "type": "array"}, {"name": "acks", "type": "array"},
                    {"name": "wait", "type": "number"}],
         "timeout": 90},
        {"name": "issue", "tool": "relay_issue", "surfaces": HIDDEN,
         "description": "A machine-channel slot for the calling node's operator daemon: "
                        "`{slot, token, expires_in}`. With `slot`, `token` and `owner`, put "
                        "that held slot back on a relay that restarted: `{restored: true}` "
                        "or `{refused: taken|ended}`.",
         "params": [{"name": "slot", "type": "string"}, {"name": "token", "type": "string"},
                    {"name": "owner", "type": "string"}],
         "timeout": 60},
        {"name": "tether", "tool": "relay_tether", "surfaces": HIDDEN,
         "description": "This host's tether relay: its release and child.", "params": []},
    ],
}

RELAY: Relay | None = None


def _relay() -> Relay:
    if RELAY is None:
        raise RuntimeError("the relay service is still starting")
    return RELAY


def node_of(args: dict, as_: str | None) -> str:
    """The calling node, once the call is shown to be that node's own service."""
    if args.get("_caller_session") is not None:
        raise RelayRefused("a session holds rooms through its own node's `channel` tool, "
                           "never through relay")
    if str(args.get("_caller_trust") or "") != "trusted":
        raise RelayRefused("only a member node's channel service calls relay")
    return edges.caller_peer(as_) or config.node_name()


def member_of(args: dict, as_: str | None) -> dict:
    node = node_of(args, as_)
    raw = args.get("member")
    if not isinstance(raw, dict) or not isinstance(raw.get("session"), str) or not raw["session"]:
        raise RelayRefused("`member` names the session this call acts for")
    name = str(raw.get("name") or "")
    return {"node": node, "session": raw["session"], "name": name,
            "level": str(raw.get("level") or "untrusted"),
            "who": f"{name or raw['session']}@{node}"}


async def open_(args: dict, as_: str | None = None) -> dict:
    return await _relay().open(member_of(args, as_), args.get("persistent") in (True, "true", 1))


async def join(args: dict, as_: str | None = None) -> dict:
    return await _relay().join(member_of(args, as_), str(args.get("name") or ""))


async def write(args: dict, as_: str | None = None) -> dict:
    message = args.get("message")
    if not isinstance(message, dict):
        raise RelayRefused("`message` is an object")
    return await _relay().write(member_of(args, as_), str(args.get("name") or ""), message)


async def close(args: dict, as_: str | None = None) -> dict:
    return await _relay().close(member_of(args, as_), str(args.get("name") or ""))


async def status(args: dict, as_: str | None = None) -> dict:
    return _relay().status(member_of(args, as_))


async def feed(args: dict, as_: str | None = None) -> dict:
    live = args.get("live")
    if live is not None and not isinstance(live, list):
        raise RelayRefused("`live` is a list of session ids, or absent")
    acks = args.get("acks") or []
    if not isinstance(acks, list) or not all(isinstance(a, dict) for a in acks):
        raise RelayRefused("`acks` is a list of {room, session, through}")
    return await _relay().feed(node_of(args, as_), None if live is None else [str(s) for s in live],
                               acks, float(args.get("wait") or 0))


RESTORE_KEYS = ("slot", "token", "owner")


async def issue(args: dict, as_: str | None = None) -> dict:
    node = node_of(args, as_)
    given = [k for k in RESTORE_KEYS if args.get(k) not in (None, "")]
    if not given:
        return await _relay().issue(node)
    if len(given) < len(RESTORE_KEYS):
        raise RelayRefused("a restore names `slot`, `token` and `owner`")
    return await _relay().issue(node, {k: str(args[k]) for k in RESTORE_KEYS})


def probe_relay(timeout: float = daemon.PROBE_TIMEOUT_S) -> tuple[str, dict[str, Any]]:
    """The relay's ``probe``: ``/health`` says whether it runs, ``/status`` what it holds.

    ``/status`` needs the issue bearer. Without it, or when it fails, the relay
    is reported ``busy``, so a replacement waits rather than risk the backlog.
    The report counts every held slot, poll and room as a live session.
    """
    base = f"http://127.0.0.1:{os.environ.get('AWM_TETHER_PORT') or 12520}"
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=timeout) as resp:
            resp.read()
    except OSError:
        return "absent", {}
    bearer = os.environ.get("AWM_TETHER_ISSUE_TOKEN", "")
    if not bearer:
        return "busy", {}
    req = urllib.request.Request(f"{base}/status", headers={"Authorization": f"Bearer {bearer}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except (OSError, ValueError):
        return "busy", {}
    held = sum(int(body.get(k) or 0) for k in ("sessions", "polled", "rooms"))
    return "answering", {"sessions": [{"phase": "open"}] * held}


CHILD: daemon.Adopted | None = None


async def tether(args: dict, as_: str | None = None) -> dict:
    node_of(args, as_)
    from awm.tether.report import release_report
    return {"role": paths.ROLE, "release": release_report(),
            "child": await asyncio.to_thread(CHILD.snapshot) if CHILD else None,
            "log": str(paths.DAEMON_LOG)}


HANDLERS = {"open": open_, "join": join, "write": write, "close": close,
            "status": status, "feed": feed, "issue": issue, "tether": tether}

LOOPS: list[asyncio.Task] = []


def build(state_dir: Path = STATE_DIR, **kw: Any) -> Relay:
    return Relay(Store(state_dir / "channels.db"), kw.pop("rooms", None) or Rooms(), **kw)


async def supervise_forever() -> None:
    """Start the tether relay binary, and keep starting it. Never returns."""
    global CHILD
    CHILD = daemon.for_role(probe_relay)
    while True:
        try:
            await asyncio.to_thread(CHILD.reconcile)
        except Exception:  # noqa: BLE001 — a bad tick must not end the loop
            log.exception("could not reconcile the tether relay")
        await asyncio.sleep(daemon.TICK_S)


def _on_start() -> None:
    relay = _relay()
    LOOPS.append(spawn_supervised("relay-tether", supervise_forever))
    relay.resume()
    LOOPS.append(spawn_supervised("relay-prune", relay.prune_forever))


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    global RELAY
    RELAY = build()
    adapter = ServiceAdapter("relay", API_MANIFEST, HANDLERS, on_start=_on_start)
    try:
        await adapter.run()
    finally:
        await RELAY.stop()
        if CHILD is not None:
            CHILD.shutdown()


if __name__ == "__main__":
    asyncio.run(main())

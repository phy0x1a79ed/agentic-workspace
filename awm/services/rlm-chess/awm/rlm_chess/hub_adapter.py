"""Hub adapter for the rlm-chess realm service.

Serves chess arena sessions (projects/game-bot/chess) through the realm-family
contract: lifecycle, perceive and act verbs over the arena's control API, and a
`chess` emitter fed by one watch stream per session this service owns.

Run via run.sh, which the gateway spawns and respawns.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from awm.rlm_chess import arena  # first: pins the arena directories
from awm.gatewayclient import ServiceAdapter
from awm.rlm_chess import dao

log = logging.getLogger("awm.rlm_chess.hub_adapter")

sessions, saves, paths, RpcError = arena.sessions, arena.saves, arena.paths, arena.RpcError

IDLE_TIMEOUT_S = float(os.environ.get("AWM_CHESS_IDLE_TIMEOUT_S", "1800"))
FINISHED_LINGER_S = float(os.environ.get("AWM_CHESS_FINISHED_LINGER_S", "900"))

START_KEYS = ("white", "black", "games", "alternate", "fen", "move_timeout_s", "seat_timeout_s",
              "max_attempts", "max_plies", "seed", "min_ply_s")


def _param(name: str, type_: str, required: bool = False) -> dict[str, Any]:
    return {"name": name, "type": type_, "required": required}


SESSION_ID = _param("session_id", "string", required=True)


def _fn(name: str, description: str, *params: dict[str, Any],
        timeout: float | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {"name": name, "tool": f"rlm_chess_{name}",
                            "description": description, "params": list(params)}
    if timeout is not None:
        spec["timeout"] = timeout
    return spec


API_MANIFEST: dict[str, Any] = {
    "functions": [
        _fn("acquire",
            "Start a chess session. opts: white and black (a bot name from `bots`, or "
            "@name for a seat played with `move`), games, alternate, fen, move_timeout_s, "
            "seat_timeout_s, max_attempts, max_plies, seed, min_ply_s (pace between plies). "
            "Or opts.load = a save name, with opts.play (default true), to resume a save. "
            "Returns {session_id, mode}; mode is pvb when a seat is external, else bvb.",
            _param("game", "string", required=True), _param("opts", "object"), timeout=120.0),
        _fn("release", "End a session. Its record stays available to `replay`.",
            SESSION_ID, timeout=60.0),
        _fn("reset",
            "Rematch: end the session and start a new one with the same players and "
            "settings. Returns {session_id, mode, previous}.",
            SESSION_ID, timeout=120.0),
        _fn("status",
            "Status of one session, or of every live session this realm owns when "
            "session_id is omitted. Returns {sessions}.",
            _param("session_id", "string")),
        _fn("observe",
            "Current {status, state} of a session. state.awaiting is the open move "
            "request and state.legal_moves the moves for the side to move.",
            SESSION_ID),
        _fn("history",
            "Moves, rejected attempts and outcome of one game (default: the current game). "
            "fens=true adds the FEN after each move.",
            SESSION_ID, _param("game_id", "string"), _param("fens", "boolean")),
        _fn("snapshot",
            "A consistent {seq, snapshot: {status, state, history}} for resyncing a live "
            "view. Events on the `chess` emitter continue after seq.",
            SESSION_ID),
        _fn("bots", "The bot names a session can seat. Returns {bots}."),
        _fn("saves",
            "Saved series ({saves}, newest first, autosave-* included) and session "
            "records ({records}, newest first).",),
        _fn("replay",
            "One game of a save (name) or of a session record (session_id), with the FEN "
            "after each move and the series' game_ids. Needs no live session.",
            _param("name", "string"), _param("session_id", "string"),
            _param("game_id", "string")),
        _fn("move",
            "Play a UCI move for the external seat to move. Returns {accepted, san}, or "
            "{accepted: false, reason, legal_moves}. Pass ply to refuse a move meant for "
            "another position.",
            SESSION_ID, _param("move", "string", required=True), _param("ply", "integer")),
        _fn("resign",
            "Resign the current game for an external seat. color defaults to the "
            "session's only external seat.",
            SESSION_ID, _param("color", "string"), timeout=60.0),
        _fn("pause", "Halt play after the move in flight.", SESSION_ID, timeout=60.0),
        _fn("resume", "Continue a paused session.", SESSION_ID),
        _fn("step", "Play exactly one ply of a paused session.", SESSION_ID, timeout=120.0),
        _fn("set_pace", "Set the minimum seconds between plies, effective at once.",
            SESSION_ID, _param("min_ply_s", "number", required=True)),
        _fn("save", "Save the series under a name. Fails with `exists` unless overwrite.",
            SESSION_ID, _param("name", "string", required=True),
            _param("overwrite", "boolean")),
    ],
    "emitters": [
        {
            "topic": "chess",
            "description": (
                "Session events. Payload {session_id, kind, seq, data}. Kinds with a seq "
                "come from the arena watch stream: phase, game_start, turn, move, rejected, "
                "game_over, pace, resync. Kinds without one come from the realm: "
                "session_started, session_ended, saved. On resync or a seq gap, call snapshot."
            ),
        },
    ],
    "sessions": [],
}


class RealmError(Exception):
    """Reaches the caller as `<code>: <message>`, so a client can branch on the code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


_ADAPTER: ServiceAdapter | None = None
_PUMPS: dict[str, asyncio.Task] = {}


async def _emit(sid: str, kind: str, seq: int | None = None,
                data: dict[str, Any] | None = None) -> None:
    if _ADAPTER is not None:
        await _ADAPTER.emit("chess", {"session_id": sid, "kind": kind, "seq": seq,
                                      "data": data or {}})


def _follow(sid: str) -> None:
    task = _PUMPS.get(sid)
    if task is None or task.done():
        _PUMPS[sid] = asyncio.create_task(_pump(sid), name=f"rlm-chess-watch-{sid}")


async def _pump(sid: str) -> None:
    since: int | None = None
    try:
        while True:
            try:
                async with sessions.watch(sid, since) as (result, frames):
                    if since is not None and "snapshot" in result:
                        await _emit(sid, "resync", result["seq"])
                    for event in result.get("events") or []:
                        await _emit(sid, event["event"], event["seq"], event.get("data"))
                    since = result["seq"]
                    async for frame in frames:
                        since = None if frame["event"] == "resync" else frame["seq"]
                        await _emit(sid, frame["event"], frame.get("seq"), frame.get("data"))
            except Exception as exc:  # noqa: BLE001 — a dead or restarting session is routine
                log.debug("watch %s: %r", sid, exc)
            if sid not in sessions.live_sessions():
                break
            await asyncio.sleep(1.0)
        await _emit(sid, "session_ended")
    finally:
        if _PUMPS.get(sid) is asyncio.current_task():
            del _PUMPS[sid]


def _sid(args: dict[str, Any]) -> str:
    sid = str(args.get("session_id") or "")
    if not paths.SID_RE.match(sid):
        raise RealmError("invalid_params", f"bad session_id {sid!r}")
    return sid


async def _call(sid: str, method: str, params: dict[str, Any] | None = None,
                timeout: float = 30.0) -> Any:
    try:
        return await sessions.call(sid, method, params, timeout=timeout)
    except RpcError as exc:
        raise RealmError(exc.code, exc.message) from exc
    except sessions.Unreachable as exc:
        raise RealmError("not_running", f"session {sid} is not running") from exc


async def _owned_status(sid: str) -> dict[str, Any] | None:
    try:
        status = await sessions.call(sid, "status", timeout=5.0)
    except (RpcError, sessions.Unreachable):
        return None
    return status if status.get("labels", {}).get("owner") == arena.OWNER else None


# ---- lifecycle ----

async def _acquire(args: dict[str, Any]) -> dict[str, Any]:
    game = str(args.get("game") or "chess")
    if game != "chess":
        raise RealmError("invalid_params", f"this realm plays chess, not {game!r}")
    opts = dict(args.get("opts") or {})
    if "load" not in opts:
        unknown = sorted(set(opts) - set(START_KEYS))
        if unknown:
            raise RealmError("invalid_params", f"unknown opts {unknown}")
        return await _start("start_game", dict(opts), opts)
    name = str(opts["load"])
    try:
        settings = saves.read(name)["settings"]
    except FileNotFoundError as exc:
        raise RealmError("not_found", str(exc)) from exc
    except ValueError as exc:
        raise RealmError("invalid_params", str(exc)) from exc
    start = {k: settings[k] for k in START_KEYS if settings.get(k) is not None}
    return await _start("load", {"name": name, "play": bool(opts.get("play", True))}, start)


async def _start(method: str, params: dict[str, Any], start: dict[str, Any]) -> dict[str, Any]:
    seats = (str(start.get("white", "")), str(start.get("black", "")))
    mode = "pvb" if any(arena.is_external(seat) for seat in seats) else "bvb"
    params |= {"labels": {"owner": arena.OWNER, "mode": mode},
               "idle_timeout_s": IDLE_TIMEOUT_S, "finished_linger_s": FINISHED_LINGER_S}
    try:
        sid, _ = await sessions.create(method, params, python=arena.PYTHON)
    except sessions.SessionError as exc:
        raise RealmError(exc.code or "start_failed", str(exc)) from exc
    dao.ChessDAO().record(sid, mode, start)
    _follow(sid)
    await _emit(sid, "session_started", data={"mode": mode})
    return {"session_id": sid, "mode": mode}


async def _release(args: dict[str, Any]) -> dict[str, Any]:
    sid = _sid(args)
    await sessions.kill(sid)
    return {"released": True, "session_id": sid}


async def _reset(args: dict[str, Any]) -> dict[str, Any]:
    sid = _sid(args)
    start = dao.ChessDAO().start_params(sid)
    if start is None:
        raise RealmError("not_found", f"session {sid} was not started by this realm")
    start.pop("seed", None)
    await sessions.kill(sid)
    return {**await _start("start_game", dict(start), start), "previous": sid}


async def _status(args: dict[str, Any]) -> dict[str, Any]:
    if args.get("session_id"):
        return {"sessions": [await _call(_sid(args), "status")]}
    found = await asyncio.gather(*(_owned_status(sid) for sid in sessions.live_sessions()))
    return {"sessions": [status for status in found if status]}


# ---- perceive ----

async def _observe(args: dict[str, Any]) -> dict[str, Any]:
    sid = _sid(args)
    status, state = await asyncio.gather(_call(sid, "status"), _call(sid, "state"))
    return {"status": status, "state": state}


async def _history(args: dict[str, Any]) -> dict[str, Any]:
    params = {k: args[k] for k in ("game_id", "fens") if args.get(k) is not None}
    return await _call(_sid(args), "history", params)


async def _snapshot(args: dict[str, Any]) -> dict[str, Any]:
    sid = _sid(args)
    try:
        async with sessions.watch(sid) as (result, _frames):
            return result
    except RpcError as exc:
        raise RealmError(exc.code, exc.message) from exc
    except OSError as exc:
        raise RealmError("not_running", f"session {sid} is not running") from exc


def _bots(args: dict[str, Any]) -> dict[str, Any]:
    return {"bots": sorted(p.parent.name.removeprefix("ai_")
                           for p in paths.SRC.glob("ai_*/__main__.py"))}


def _saves(args: dict[str, Any]) -> dict[str, Any]:
    return {"saves": saves.summaries(), "records": saves.record_summaries()}


def _replay(args: dict[str, Any]) -> dict[str, Any]:
    name = args.get("name")
    if bool(name) == bool(args.get("session_id")):
        raise RealmError("invalid_params", "pass exactly one of name and session_id")
    try:
        data = saves.read(str(name)) if name else saves.read_record(_sid(args))
        return saves.replay(data, args.get("game_id"))
    except (FileNotFoundError, LookupError) as exc:
        raise RealmError("not_found", str(exc)) from exc
    except ValueError as exc:
        raise RealmError("invalid_params", str(exc)) from exc


# ---- act ----

async def _move(args: dict[str, Any]) -> dict[str, Any]:
    sid = _sid(args)
    waiting = (await _call(sid, "state"))["awaiting"]
    if not (waiting and waiting["external"]):
        raise RealmError("not_your_turn", "no external seat is to move")
    if args.get("ply") is not None and int(args["ply"]) != waiting["ply"]:
        raise RealmError("stale", f"the position is at ply {waiting['ply']}, not {args['ply']}")
    return await _call(sid, "submit_move", {"color": waiting["color"], "ply": waiting["ply"],
                                            "game_id": waiting["game_id"],
                                            "move": str(args["move"])})


async def _resign(args: dict[str, Any]) -> dict[str, Any]:
    sid = _sid(args)
    color = args.get("color")
    if not color:
        external = [b["color"] for b in (await _call(sid, "status"))["bots"] if b["external"]]
        if len(external) != 1:
            raise RealmError("invalid_params", "pass color: the session has no single external seat")
        color = external[0]
    return await _call(sid, "resign", {"color": color}, timeout=60.0)


def _control(method: str, *keys: str, timeout: float = 30.0):
    async def handler(args: dict[str, Any]) -> Any:
        params = {k: args[k] for k in keys if args.get(k) is not None}
        return await _call(_sid(args), method, params, timeout)
    return handler


async def _save(args: dict[str, Any]) -> dict[str, Any]:
    sid = _sid(args)
    params = {"name": args.get("name"), "overwrite": bool(args.get("overwrite", False))}
    result = await _call(sid, "save", params)
    await _emit(sid, "saved", data={"name": params["name"]})
    return result


HANDLERS = {
    "acquire": _acquire,
    "release": _release,
    "reset": _reset,
    "status": _status,
    "observe": _observe,
    "history": _history,
    "snapshot": _snapshot,
    "bots": _bots,
    "saves": _saves,
    "replay": _replay,
    "move": _move,
    "resign": _resign,
    "pause": _control("stop", timeout=60.0),
    "resume": _control("resume"),
    "step": _control("step", timeout=120.0),
    "set_pace": _control("set_pace", "min_ply_s"),
    "save": _save,
}


async def _adopt() -> None:
    """Re-watch the sessions this realm started, which outlive a service restart."""
    for sid in sessions.live_sessions():
        if await _owned_status(sid):
            _follow(sid)


async def _on_start() -> None:
    dao.init()
    await _adopt()


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    global _ADAPTER
    _ADAPTER = ServiceAdapter("rlm-chess", API_MANIFEST, HANDLERS, on_start=_on_start)
    await _ADAPTER.run()


if __name__ == "__main__":
    asyncio.run(main())

"""dsh's side of the harness contract (harness-contract.md § dsh): the fork's sessions.

The fork serves one RPC API on loopback. Every call is ``POST /api/<method>``
with ``{type: "client-request", rpcId, method, payload}``, answered with HTTP
200 and ``result: {ok, value | error}``. It has no authentication. A call
passes only from loopback, with a JSON content type and no cross-site Origin.

A fork session id is ``session-<uuid>``. Its R9 id is ``ds`` plus the 32 hex
digits of that UUID. A verb the fork cannot serve answers ``unsupported``.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from awm.dsh import harness
from awm.gatewayclient import ids

log = logging.getLogger("awm.dsh.sessions")

PLATFORM = "ds"
CONTRACT = "harness@1"
COMPACT_WAIT_S = 1800
POLL_S = 3.0


class ForkError(RuntimeError):
    """The fork refused the call, or did not answer it."""


# Overridable for tests: (method, payload) -> value.
_rpc: Optional[Callable[[str, dict], Any]] = None
# Overridable for tests: (service, fn, args) -> result.
_event: Optional[Callable[[str, str, dict], Any]] = None


def rpc(method: str, payload: dict) -> Any:
    if _rpc is not None:
        return _rpc(method, payload)
    import httpx

    body = {"type": "client-request", "rpcId": uuid.uuid4().hex, "method": method,
            "payload": payload}
    try:
        resp = httpx.post(f"http://127.0.0.1:{harness.PORT}/api/{method}", json=body,
                          timeout=30.0)
        result = resp.json().get("result") or {}
    except Exception as exc:  # noqa: BLE001 - a down fork reads the same as a refusal
        raise ForkError(f"the DeepSeek harness did not answer {method}: {exc}") from exc
    if not result.get("ok"):
        err = result.get("error") or {}
        raise ForkError(f"{method}: {err.get('code')}: {err.get('message')}")
    return result.get("value")


def r9(fork_id: str) -> Optional[str]:
    try:
        return ids.r9(PLATFORM, (fork_id or "").removeprefix("session-"))
    except ids.BadSessionId:
        return None


def _unsupported(what: str) -> dict:
    return {"ok": False, "unsupported": True,
            "error": f"the DeepSeek harness cannot {what}"}


def all_sessions() -> list[dict]:
    rows, cursor = [], None
    for _ in range(50):
        page = rpc("session.list", {"cursor": cursor} if cursor else {}) or {}
        rows += page.get("items") or []
        cursor = page.get("nextCursor") or page.get("cursor")
        if not cursor:
            break
    return rows


def _row(item: dict) -> Optional[dict]:
    sid = r9(item.get("sessionId") or "")
    if sid is None:
        return None
    cwd = item.get("cwd")
    where = _scope_of(cwd)
    return {"session_id": sid, "fork_session_id": item["sessionId"],
            "name": item.get("title") or None, "live": True,
            "state": "busy" if item.get("running") else "idle", "cwd": cwd,
            "project": where[0] if where else None, "scope": where[1] if where else None,
            "parent": r9(item.get("parentSessionId") or "") if item.get("parentSessionId") else None,
            "preset": item.get("agentPreset")}


def _scope_of(cwd: Optional[str]) -> Optional[tuple[str, str]]:
    parts = Path(cwd).parts if cwd else ()
    if "projects" not in parts:
        return None
    rest = parts[len(parts) - parts[::-1].index("projects"):]
    return (rest[0], "/".join(rest[1:])) if len(rest) >= 2 else None


def rows() -> list[dict]:
    return [r for r in (_row(i) for i in all_sessions()) if r]


def find_one(ref: str) -> Optional[dict]:
    ref = (ref or "").strip().lower()
    if not ref:
        return None
    hits = [r for r in rows() if ref in (r["session_id"], r["fork_session_id"].lower())
            or (len(ref) >= 8 and r["session_id"].startswith(ref))
            or (r["name"] or "").lower() == ref]
    if len(hits) > 1:
        raise ForkError(f"{ref!r} names {len(hits)} DeepSeek harness sessions")
    return hits[0] if hits else None


# --- who is calling -------------------------------------------------------------


def _environ(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    out = {}
    for item in raw.split(b"\0"):
        key, _, value = item.partition(b"=")
        if key:
            out[key.decode(errors="replace")] = value.decode(errors="replace")
    return out


def _ppid(pid: int) -> int:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rpartition(")")[2].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def resolve(pid: Any, fork_pid: Optional[int]) -> dict:
    """Whether ``pid`` is a fork session, walking up to the fork's node process."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {"ok": True, "mine": False, "error": "no pid"}
    seen = 0
    while pid > 1 and seen < 64:
        if fork_pid and pid == fork_pid:
            return _the_running_session(pid)
        fork_id = _environ(pid).get("DSH_SESSION_ID")
        if fork_id:
            sid = r9(fork_id)
            return {"ok": True, "mine": True, "session_id": sid, "hosting": "web",
                    "fork_session_id": fork_id, "pid": pid, "job": None}
        pid, seen = _ppid(pid), seen + 1
    return {"ok": True, "mine": False}


def _the_running_session(fork_pid: int) -> dict:
    """The caller behind the fork's one MCP client: the only session with a turn running.

    The client carries no session id. A tool call happens inside a turn, so when
    exactly one session runs, it is the caller. With none or several, dsh cannot tell.
    """
    try:
        running = [r for r in rows() if r["state"] == "busy"]
    except ForkError as exc:
        running, why = [], f"the fork did not list its sessions: {exc}"
    else:
        why = (f"{len(running)} sessions have a turn running" if running
               else "no session has a turn running")
    if len(running) == 1:
        row = running[0]
        return {"ok": True, "mine": True, "session_id": row["session_id"], "hosting": "web",
                "fork_session_id": row["fork_session_id"], "name": row["name"],
                "cwd": row["cwd"], "pid": fork_pid, "job": None}
    return {"ok": True, "mine": True, "session_id": None,
            "error": ("the call came through the DeepSeek harness's one MCP client, "
                      f"which carries no session id, and {why}, so dsh cannot tell "
                      "which session called")}


# --- verbs ----------------------------------------------------------------------


def target(args: dict, fork_pid: Optional[int]) -> dict:
    ref = args.get("session_id") or args.get("ref")
    if ref:
        row = find_one(str(ref))
    elif args.get("pid"):
        who = resolve(args["pid"], fork_pid)
        if not who.get("session_id"):
            raise ForkError(who.get("error") or "the caller is no DeepSeek harness session")
        row = find_one(who["session_id"])
    else:
        raise ForkError("name a session with session_id or ref")
    if row is None:
        raise ForkError(f"no DeepSeek harness session matches {ref!r}")
    return row


def _text(text: str) -> list[dict]:
    return [{"type": "text", "text": text}]


def send(row: dict, text: str) -> dict:
    if text.startswith("/"):
        rpc("commands/execute", {"agentId": row["fork_session_id"], "line": text})
        return {"ok": True, "submitted": True, "confirmed": "executed",
                "session": row["session_id"], "text": text}
    rpc("session.prompt", {"sessionId": row["fork_session_id"], "mode": "queue",
                           "content": _text(text)})
    return {"ok": True, "submitted": True, "confirmed": "enqueued",
            "session": row["session_id"], "text": text}


def interrupt(row: dict) -> dict:
    rpc("session.cancel", {"sessionId": row["fork_session_id"]})
    return {"ok": True, "interrupted": True}


def stop(row: dict) -> dict:
    rpc("session.cancel", {"sessionId": row["fork_session_id"]})
    return {"ok": True, "stopped": True, "unloaded": False,
            "note": "the fork has no route that unloads a session; its turn is cancelled"}


def set_(row: dict, args: dict, provider: Optional[str]) -> dict:
    if args.get("cwd"):
        return _unsupported("move a session: its folder is fixed when it is created")
    applied: dict[str, Any] = {}
    if args.get("model") or args.get("effort"):
        payload = {"sessionId": row["fork_session_id"], "provider": provider,
                   "model": args.get("model")}
        if args.get("effort"):
            payload["reasoningEffort"] = args["effort"]
        applied["model"] = rpc("session.selectModel", payload)
    if args.get("name"):
        applied["name"] = rpc("session.rename", {"sessionId": row["fork_session_id"],
                                                 "title": args["name"]})
    if not applied:
        return {"ok": False, "error": "set needs one of model, effort or name"}
    return {"ok": True, "applied": applied}


def start(args: dict, *, workspace: Path, provider: Optional[str]) -> dict:
    project, scope = args.get("project"), args.get("scope")
    if not (project and scope):
        return {"ok": False, "reason": "start needs project and scope"}
    if args.get("pin") or args.get("isolated"):
        return _unsupported("pin or isolate a session")
    cwd = workspace / "projects" / project / scope
    if not cwd.is_dir():
        return {"ok": False, "reason": f"no worktree at {cwd}"}
    fork_id = f"session-{uuid.uuid4()}"
    payload: dict[str, Any] = {"cwd": str(cwd), "sessionId": fork_id}
    if args.get("preset"):
        payload["agentPreset"] = args["preset"]
    rpc("session.create", payload)
    row = {"session_id": r9(fork_id), "fork_session_id": fork_id}
    if args.get("name") or args.get("model") or args.get("effort"):
        set_(row, {"name": args.get("name"), "model": args.get("model"),
                   "effort": args.get("effort")}, provider)
    if args.get("prompt"):
        send(row, args["prompt"])
    return {"ok": True, "session_id": row["session_id"], "fork_session_id": fork_id,
            "job": None, "name": args.get("name"), "cwd": str(cwd)}


def resume(row: dict, prompt: Optional[str]) -> dict:
    if prompt:
        send(row, prompt)
    return {"ok": True, "session_id": row["session_id"], "job": None, "live": True}


# --- compact --------------------------------------------------------------------


def _events_since(fork_id: str, seq: Optional[int]) -> list[dict]:
    page = rpc("session.history", {"sessionId": fork_id, "maxMessages": 200}) or {}
    events = page.get("events") or []
    return [e for e in events if seq is None or (e.get("seq") or 0) > seq]


def _last_seq(fork_id: str) -> Optional[int]:
    events = _events_since(fork_id, None)
    seqs = [e.get("seq") for e in events if isinstance(e.get("seq"), int)]
    return max(seqs) if seqs else None


def _kind(event: dict) -> str:
    return str(event.get("type") or event.get("kind") or "")


def _report(event: dict) -> None:
    try:
        if _event is not None:
            _event("reflection", "harness_event", event)
            return
        from awm.gatewayclient import call_sync
        call_sync("reflection", "harness_event", event, timeout=15.0)
    except Exception as exc:  # noqa: BLE001
        log.error("dsh: reflection did not take the %s event for %s: %s",
                  event.get("kind"), event.get("session_id"), exc)


def watch_compaction(row: dict, followup: str, ticket: str, since: Optional[int], *,
                     sleep: Callable[[float], None] = time.sleep,
                     clock: Callable[[], float] = time.monotonic) -> str:
    """Wait for ``compaction/end``, queue the follow-up, and report how it ended."""
    fork_id = row["fork_session_id"]
    base = {"session_id": row["session_id"], "ticket": ticket, "command": "/compact",
            "followup": followup, "cwd": row.get("cwd"), "name": row.get("name") or ""}
    deadline = clock() + COMPACT_WAIT_S
    ended = None
    while ended is None:
        if clock() >= deadline:
            _report({**base, "kind": "lost", "reason": "the compaction did not end "
                     f"within {COMPACT_WAIT_S // 60} minutes"})
            return "lost"
        try:
            ended = next((e for e in _events_since(fork_id, since)
                          if _kind(e) == "compaction/end"), None)
        except ForkError as exc:
            log.warning("dsh: %s", exc)
        if ended is None:
            sleep(POLL_S)
    error = (ended.get("payload") or ended).get("error")
    if error:
        _report({**base, "kind": "lost", "reason": f"the compaction failed: {error}"})
        return "lost"
    try:
        send(row, followup)
    except ForkError as exc:
        _report({**base, "kind": "lost", "reason": f"the follow-up did not go in: {exc}"})
        return "lost"
    _report({**base, "kind": "done"})
    return "done"


def compact(row: dict, followup: Optional[str], *,
            spawn: Callable[[Callable[[], None]], Any] | None = None) -> dict:
    if row.get("preset") == "minimal":
        return _unsupported("compact a session of the minimal preset")
    since = _last_seq(row["fork_session_id"])
    send(row, "/compact")
    out = {"ok": True, "submitted": True, "text": "/compact", "followup": followup}
    if followup:
        ticket = uuid.uuid4().hex
        run = spawn or (lambda fn: threading.Thread(target=fn, daemon=True,
                                                    name="dsh-compact").start())
        run(lambda: watch_compaction(row, followup, ticket, since))
        out.update(ticket=ticket, followup_deferred=True)
    return out


def platform() -> dict:
    return {"ok": True, "code": PLATFORM, "name": "DeepSeek harness",
            "contract": CONTRACT, "capabilities": []}


def workspace_root() -> Path:
    return Path(os.environ.get("AWM_WORKSPACE") or Path.home() / "agentic_workspace")


def provider_name() -> Optional[str]:
    from awm.dsh import settings
    try:
        sel = settings.selection()
    except Exception:  # noqa: BLE001
        return None
    return sel.get("provider") or None


"""Find the harness plugin of a session and call it (harness-contract.md).

A provider is a tool that implements the contract ``harness@1``. The gateway
lists them live at ``GET /hub/contract/harness@1``, and each says its platform
code through the contract's ``platform`` verb. A call goes through
``POST /invoke``. reflection names no provider itself (install.md R117 item 4).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from awm.gatewayclient import ids

log = logging.getLogger("awm.reflection.harness")

CONTRACT = "harness@1"
PLUGIN = "reflection"

# A provider that fails a call is skipped for this long, so a node without dsh
# does not pay a failed call on every reflection verb.
DOWN_FOR_S = 60.0
# The provider list and each provider's platform code are read again after this.
TABLE_FOR_S = 30.0


class HarnessError(RuntimeError):
    """No harness plugin could do what was asked."""


class Ambiguous(HarnessError):
    """A ref names sessions under more than one harness plugin."""


@dataclass(frozen=True)
class Provider:
    tool: str
    code: Optional[str]
    peer: Optional[str] = None


# Overridable for tests: (provider, verb, args, as_) -> result dict.
_call: Optional[Callable[[Provider, str, dict, Optional[str]], dict]] = None

_down: dict[str, float] = {}
_codes: dict[tuple[str, Optional[str]], str] = {}
_table: tuple[float, list[dict]] = (0.0, [])
_lock = threading.Lock()


def _hub() -> str:
    return os.environ.get("AWM_HUB_URL", "http://127.0.0.1:8765").rstrip("/")


def _contract_rows() -> list[dict]:
    """The live providers of the contract, read at most every ``TABLE_FOR_S``."""
    global _table
    with _lock:
        at, rows = _table
    if rows and time.monotonic() - at < TABLE_FOR_S:
        return rows
    import httpx

    try:
        resp = httpx.get(f"{_hub()}/hub/contract/{CONTRACT}",
                         headers={"X-Awm-Plugin": PLUGIN}, timeout=10.0)
        resp.raise_for_status()
        got = resp.json()
    except Exception as exc:  # noqa: BLE001 - no table is no provider
        log.warning("reflection: cannot list the providers of %s: %s", CONTRACT, exc)
        return rows
    rows = [r for r in got if isinstance(r, dict) and r.get("name")
            and int(r.get("major") or 1) == 1] if isinstance(got, list) else []
    with _lock:
        _table = (time.monotonic(), rows)
    return rows


def _code_of(tool: str, peer: Optional[str]) -> Optional[str]:
    """The platform code ``tool`` serves, from its own ``platform`` verb."""
    key = (tool, peer)
    if key in _codes:
        return _codes[key]
    try:
        out = call(Provider(tool, None, peer), "platform", {})
    except HarnessError as exc:
        log.info("reflection: %s", exc)
        return None
    code = out.get("code")
    if isinstance(code, str) and code in ids.PLATFORMS:
        _codes[key] = code
        return code
    return None


def providers() -> list[Provider]:
    """Every harness plugin reflection may call, the ones marked down left out."""
    now = time.monotonic()
    out = []
    for row in _contract_rows():
        tool, peer = str(row["name"]), row.get("peer") or None
        with _lock:
            if _down.get(tool, 0.0) > now:
                continue
        code = _code_of(tool, peer)
        if code:
            out.append(Provider(tool, code, peer))
    return out


def for_platform(code: str) -> Provider:
    for p in providers():
        if p.code == code:
            return p
    raise HarnessError(f"no harness plugin for platform {code!r} runs on this node")


def _invoke(p: Provider, verb: str, args: dict, as_: Optional[str]) -> dict:
    import httpx

    body: dict[str, Any] = {"verb": verb, "args": args}
    if p.peer:
        body["peer"] = p.peer
    headers = {"Content-Type": "application/json", "X-Awm-Plugin": PLUGIN}
    if as_:
        headers["X-Awm-As"] = as_
    resp = httpx.post(f"{_hub()}/invoke", json={"name": p.tool, "args": body},
                      headers=headers, timeout=_timeout(verb))
    if resp.status_code == 503 or (resp.status_code >= 500 and "not up" in resp.text):
        # The table lists a provider whether or not it runs here: this one is down.
        raise ConnectionError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    if resp.status_code >= 300:
        raise HarnessError(f"{p.tool} {verb}: HTTP {resp.status_code}: "
                           f"{resp.text[:300]}")
    result = resp.json().get("result")
    return json.loads(result) if isinstance(result, str) else (result or {})


def _timeout(verb: str) -> float:
    return {"start": 1800.0, "revive_pinned": 600.0, "resume": 90.0,
            "set": 120.0}.get(verb, 60.0)


def call(p: Provider, verb: str, args: dict, *, as_: Optional[str] = None) -> dict:
    """``verb`` on provider ``p``. A transport failure marks it down and raises."""
    clean = {k: v for k, v in args.items() if v is not None}
    try:
        if _call is not None:
            out = _call(p, verb, clean, as_)
        else:
            out = _invoke(p, verb, clean, as_)
    except HarnessError:
        raise
    except Exception as exc:  # noqa: BLE001 - every transport fault reads the same
        with _lock:
            _down[p.tool] = time.monotonic() + DOWN_FOR_S
        raise HarnessError(f"{p.tool} did not answer {verb}: {exc}") from exc
    return out if isinstance(out, dict) else {"ok": True, "result": out}


def _ask_all(verb: str, args: dict) -> list[tuple[Provider, dict]]:
    out = []
    for p in providers():
        try:
            out.append((p, call(p, verb, args)))
        except HarnessError as exc:
            log.info("reflection: %s", exc)
    return out


def for_session(session_id: str) -> tuple[Provider, dict]:
    """The provider of an R9 ``session_id``, and what it says about that session."""
    p = for_platform(session_id[:2])
    who = call(p, "status", {"session_id": session_id})
    if who.get("ok") is False:
        raise HarnessError(who.get("reason") or who.get("error")
                           or f"{p.tool} does not know session {session_id}")
    return p, {**who, "session_id": session_id}


def for_caller(pid: Any, session_id: Optional[str] = None) -> tuple[Provider, dict]:
    """The provider that owns the caller, and what it says about the caller.

    An isolated session has no host pid: the gateway names it by its R9
    ``session_id`` (plugin-contract.md § Isolated sessions).
    """
    if session_id:
        return for_session(session_id)
    if not pid:
        raise HarnessError(
            "could not tell which session is calling, so there is no session of "
            "your own to act on. Call through a session's own awm-mcp proxy, or "
            "name a session with `job`")
    answers = [(p, r) for p, r in _ask_all("resolve", {"pid": pid}) if r.get("mine")]
    if not answers:
        raise HarnessError(f"no harness plugin on this node knows pid {pid} as "
                           f"a session")
    if len(answers) > 1:
        raise Ambiguous(f"pid {pid} is claimed by {', '.join(p.tool for p, _ in answers)}")
    p, who = answers[0]
    if not who.get("session_id"):
        raise HarnessError(who.get("error") or f"{p.tool} owns pid {pid} but "
                           f"cannot tell which of its sessions is calling")
    return p, who


def for_ref(ref: str) -> tuple[Provider, dict]:
    """The provider whose session ``ref`` names, and the argument that names it there."""
    text = ref.strip()
    if ids.is_r9(text.lower()) and len(text) > 8:
        return for_platform(text[:2].lower()), {"session_id": text.lower()}
    hits = [(p, r) for p, r in _ask_all("find", {"ref": text}) if r.get("matches")]
    if not hits:
        return _unknown(text)
    if len(hits) > 1:
        raise Ambiguous(f"{text!r} names sessions under "
                        f"{', '.join(p.tool for p, _ in hits)}; pass the session id")
    return hits[0][0], {"ref": text}


class UnknownSession(HarnessError):
    def __init__(self, ref: str) -> None:
        self.ref = ref
        super().__init__(f"no session on this node has the job id or name {ref!r}; "
                         f"`list_other` shows them")


def _unknown(ref: str):
    raise UnknownSession(ref)


def target(job: Optional[str], pid: Any,
           session_id: Optional[str] = None) -> tuple[Provider, dict]:
    """The provider and target arguments for a verb that takes an optional ``job``."""
    ref = job.strip() if isinstance(job, str) else ""
    if ref:
        p, named = for_ref(ref)
        return p, {**named, "pid": pid}
    if session_id:
        p, _who = for_session(session_id)
        return p, {"session_id": session_id}
    p, _who = for_caller(pid)
    return p, {"pid": pid}


def everyone(verb: str, args: dict, *, as_: Optional[str] = None
             ) -> list[tuple[Provider, dict]]:
    """``verb`` on every provider that answers it."""
    out = []
    for p in providers():
        try:
            out.append((p, call(p, verb, args, as_=as_)))
        except HarnessError as exc:
            log.info("reflection: %s", exc)
    return out

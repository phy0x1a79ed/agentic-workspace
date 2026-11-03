"""Find the harness plugin of a session and call it (harness-contract.md).

A provider is a tool that implements the harness contract. Under the plugin
loader the gateway lists them in ``AWM_RESOLVED_TOOLS`` under ``contracts``, and
a call goes through ``POST /invoke``. Under today's gateway the providers are the
known services, and a call goes through ``/svc/<service>/fn/<verb>``.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from awm.reflection import ids

log = logging.getLogger("awm.reflection.harness")

CONTRACT_UUID = "8ca45e2d-96cb-497f-8e00-e4cb25ba0fd3"
CONTRACT_MAJOR = 1
PLUGIN = "reflection"

# Today's gateway: the tool, the service that serves it, its platform code, and
# the prefix of its internal function names. dsh serves its own verbs too, so its
# contract verbs are `harness_<verb>` behind `/svc/dsh/fn/`.
KNOWN = (("harness-cc", "cch", ids.PLATFORM_CLAUDE, ""),
         ("harness-ds", "dsh", ids.PLATFORM_DEEPSEEK, "harness_"))

# A provider that fails a call is skipped for this long, so a node without dsh
# does not pay a failed call on every reflection verb.
DOWN_FOR_S = 60.0


class HarnessError(RuntimeError):
    """No harness plugin could do what was asked."""


class Ambiguous(HarnessError):
    """A ref names sessions under more than one harness plugin."""


@dataclass(frozen=True)
class Provider:
    tool: str
    service: Optional[str]
    code: Optional[str]
    peer: Optional[str] = None
    prefix: str = ""


# Overridable for tests: (provider, verb, args, as_) -> result dict.
_call: Optional[Callable[[Provider, str, dict, Optional[str]], dict]] = None

_down: dict[str, float] = {}
_lock = threading.Lock()


def _resolved_providers() -> Optional[list[Provider]]:
    path = os.environ.get("AWM_RESOLVED_TOOLS")
    if not path:
        return None
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except (OSError, ValueError) as exc:
        log.warning("reflection: cannot read AWM_RESOLVED_TOOLS %s: %s", path, exc)
        return None
    rows = (data.get("contracts") or {}).get(CONTRACT_UUID) or []
    codes = {tool: code for tool, _svc, code, _pre in KNOWN}
    return [Provider(r["name"], None, codes.get(r["name"]), r.get("peer"))
            for r in rows
            if isinstance(r, dict) and r.get("name")
            and int(r.get("major") or CONTRACT_MAJOR) == CONTRACT_MAJOR]


def providers() -> list[Provider]:
    """Every harness plugin reflection may call, the ones marked down left out."""
    found = _resolved_providers()
    if found is None:
        found = [Provider(tool, svc, code, None, pre) for tool, svc, code, pre in KNOWN]
    now = time.monotonic()
    with _lock:
        return [p for p in found if _down.get(p.tool, 0.0) <= now]


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
    hub = os.environ.get("AWM_HUB_URL", "http://127.0.0.1:8765").rstrip("/")
    resp = httpx.post(f"{hub}/invoke", json={"name": p.tool, "args": body},
                      headers=headers, timeout=_timeout(verb))
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
        elif p.service:
            from awm.gatewayclient import call_sync
            out = call_sync(p.service, p.prefix + verb, clean, as_=as_,
                            timeout=_timeout(verb))
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


def for_caller(pid: Any) -> tuple[Provider, dict]:
    """The provider that owns the caller pid, and what it says about the caller."""
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


def target(job: Optional[str], pid: Any) -> tuple[Provider, dict]:
    """The provider and target arguments for a verb that takes an optional ``job``."""
    ref = job.strip() if isinstance(job, str) else ""
    if ref:
        p, named = for_ref(ref)
        return p, {**named, "pid": pid}
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

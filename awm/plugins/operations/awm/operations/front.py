"""The ``operations`` verbs a session calls on its own node (queue.md R145, fleet.md R106).

A verb names the calling session by the pid the gateway stamped on the call, which
reflection turns into the session's R9 id (sessions.md R9). A service with no session
passes ``session_id`` instead, except where only the session itself may act: an approval
request records the calling session, and only that session cancels it (fleet.md R97
item 1). Every write goes through the relay, so it waits in the outbox while alcor is
away.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from awm.reflection.ids import normalise

from awm.operations.relay import Refused, Relay, Unreachable

log = logging.getLogger("awm.operations.front")

NEEDS_YOU_STATES = ("planned", "blocked", "review")
TITLE_CHARS = 80

SessionOf = Callable[[int], Awaitable[dict | None]]
Tell = Callable[[str | None, str, str], Awaitable[bool]]
Dm = Callable[[str], Awaitable[bool]]


def _unwrap(out: Any) -> Any:
    """A read's answer, without the relay's stale wrapper."""
    if isinstance(out, dict) and out.get("stale") is True and "result" in out:
        return out["result"]
    return out


def _title(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= TITLE_CHARS else text[:TITLE_CHARS - 1].rsplit(" ", 1)[0] + "…"


@dataclass
class Caller:
    session_id: str | None
    job: str | None = None
    name: str | None = None


@dataclass
class Front:
    relay: Relay
    node: str
    session_of: SessionOf
    tell: Tell
    dm: Dm
    arbutus: Any = None

    # -- who is calling --------------------------------------------------------

    async def _caller(self, args: dict) -> Caller:
        pid = args.pop("_caller_pid", None)
        rec = await self.session_of(pid) if isinstance(pid, int) else None
        if not rec:
            return Caller(None)
        return Caller(normalise(rec["session_id"]), rec.get("job"), rec.get("name"))

    async def _session(self, args: dict, *, required: bool = False) -> tuple[str | None, Caller]:
        caller = await self._caller(args)
        given = args.pop("session_id", None)
        session = normalise(given) if given else caller.session_id
        if required and not session:
            raise ValueError("no calling session: pass session_id")
        return session, caller

    async def _own(self, args: dict) -> Caller:
        """The calling session itself. A session_id argument may only repeat it."""
        caller = await self._caller(args)
        given = args.pop("session_id", None)
        if not caller.session_id:
            raise ValueError("this verb is for a running session: no calling session")
        if given and normalise(given) != caller.session_id:
            raise ValueError("session_id does not match the calling session")
        return caller

    async def _task_of(self, session: str | None) -> str | None:
        if not session:
            return None
        return _unwrap(await self.relay.read("task_for_session", {"session_id": session}))

    # -- the task verbs (sessions.md R57, R58) -----------------------------------

    async def task_open(self, args: dict) -> dict:
        session, caller = await self._session(args)
        title = str(args.get("title") or caller.name or "").strip()
        out = await self.relay.write("task_open", {
            "title": title, "session_id": session, "peer": args.get("peer"),
            "tags": args.get("tags"), "name": args.get("name") or caller.name,
            "rc_url": args.get("rc_url")})
        return {"ok": True, **out}

    async def task_attach(self, args: dict) -> dict:
        session, caller = await self._session(args, required=True)
        out = await self.relay.write("task_attach", {
            "task_id": args.get("task_id"), "session_id": session,
            "role": args.get("role") or "worker", "name": args.get("name") or caller.name,
            "rc_url": args.get("rc_url")})
        return {"ok": True, **out}

    async def task_join(self, args: dict) -> dict:
        session, _ = await self._session(args, required=True)
        out = await self.relay.write("task_join", {"task_id": args.get("task_id"),
                                                   "session_id": session})
        main = out.get("main_session_id")
        told = False
        if main and main != session and out.get("main_name"):
            told = await self.tell(out.get("main_node"), out["main_name"],
                                   f"[awm] session {session} joined your task {out['task_id']} "
                                   "(operations task_sessions shows it).")
        return {"ok": True, **out, "main_notified": told}

    async def task_report(self, args: dict) -> dict:
        session, _ = await self._session(args)
        task_id = args.get("task_id") or await self._task_of(session)
        if not task_id:
            raise ValueError("no task: pass task_id, or open one with task_open")
        state = str(args.get("state") or "")
        out = await self.relay.write("task_report", {
            "task_id": task_id, "session_id": session, "state": state,
            "reason": args.get("reason"), "rc_url": args.get("rc_url")})
        if session:
            self.relay.mark(f"needs:{session}", task_id) if state in NEEDS_YOU_STATES \
                else self.relay.mark(f"needs:{session}", clear=True)
        return {"ok": True, **out}

    async def task_session_end(self, args: dict) -> dict:
        session, _ = await self._session(args, required=True)
        out = await self.relay.write("task_session_end", {"task_id": args.get("task_id"),
                                                          "session_id": session})
        return {"ok": True, **out}

    async def task_for_session(self, args: dict) -> dict:
        session, _ = await self._session(args, required=True)
        return {"ok": True, "session_id": session, "task_id": await self._task_of(session)}

    async def task_sessions(self, args: dict) -> dict:
        return {"ok": True, "task_id": args.get("task_id"),
                "sessions": _unwrap(await self.relay.read("task_sessions",
                                                          {"task_id": args.get("task_id")}))}

    async def task_get(self, args: dict) -> dict:
        return {"ok": True, "task": _unwrap(await self.relay.read(
            "task_get", {"task_id": args.get("task_id")}))}

    async def task_list(self, args: dict) -> dict:
        keep = {k: args[k] for k in ("section", "q", "before", "limit") if args.get(k) is not None}
        return {"ok": True, **_unwrap(await self.relay.read("task_list", keep))}

    # -- Tony (queue.md R61 item 2) ------------------------------------------------

    async def _card_for(self, session: str | None, caller: Caller, reason: str) -> str:
        task_id = await self._task_of(session)
        if task_id:
            return task_id
        out = await self.relay.write("task_open", {
            "title": caller.name or _title(reason) or "A session needs Tony",
            "session_id": session, "name": caller.name})
        return out["task_id"]

    async def request_human(self, args: dict) -> dict:
        """Tell Tony you need him: blocked, review, or an Arbutus admin approval."""
        kind = str(args.get("kind") or "").strip() or (
            "review" if args.get("summary") and not args.get("reason") else "blocked")
        session, caller = await self._session(args)
        if kind == "approval":
            return await self._arbutus(args, session, caller)
        if kind not in ("blocked", "review"):
            raise ValueError("kind must be blocked, review or approval")
        text = str(args.get("reason") if kind == "blocked" else
                   args.get("summary") or args.get("reason") or "").strip()
        if not text:
            raise ValueError("reason is required" if kind == "blocked" else "summary is required")
        task_id = await self._card_for(session, caller, text)
        out = await self.task_report({"task_id": task_id, "session_id": session, "state": kind,
                                      "reason": text})
        if kind == "blocked" and args.get("urgent") is True:
            out["notified"] = await self.dm(
                f"**{caller.name or task_id}** on **{self.node}** needs you.\n{text}\n"
                f"Card {task_id}. Attach: `claude attach {caller.job or caller.name or '<job>'}` "
                f"on {self.node}")
        return out

    async def _arbutus(self, args: dict, session: str | None, caller: Caller) -> dict:
        if self.arbutus is None:
            raise ValueError("this node takes no admin approvals")
        req = await self.arbutus.open(str(args.get("bundle") or ""),
                                      agent=caller.name or caller.job or "a session",
                                      node=self.node, job=caller.job or "")
        reason = f"Admin approval {req['name']} (ref {req['ref']}) waits for Tony or Ryan in Slack."
        task_id = await self._card_for(session, caller, reason)
        out = await self.task_report({"task_id": task_id, "session_id": session,
                                      "state": "blocked", "reason": reason})
        self.arbutus.attach(req["ref"], task_id)
        return {**out, "approval": req["ref"],
                "notice": "A line is typed into this session when an admin decides."}

    async def arbutus_decided(self, task_id: str, line: str) -> None:
        """An Arbutus decision moves its card back to Working."""
        await self.relay.write("task_report", {"task_id": task_id, "state": "working",
                                               "reason": line})

    async def hook_prompt(self, args: dict) -> dict:
        """Tony's prompt to a session moves its card from Needs you to Working (R61 item 2)."""
        caller = await self._caller(args)
        if not caller.session_id:
            return {"ok": True, "moved": False}
        task_id = self.relay.mark(f"needs:{caller.session_id}")
        if not task_id:
            return {"ok": True, "moved": False}
        await self.task_report({"task_id": task_id, "session_id": caller.session_id,
                                "state": "working"})
        return {"ok": True, "moved": True, "task_id": task_id}

    # -- approvals (fleet.md R97) -------------------------------------------------------

    async def approval_request(self, args: dict) -> dict:
        caller = await self._caller(args)
        args.pop("session_id", None)  # a request is the calling session's, never a named one
        out = await self.relay.write("approval_request", {
            "kind": args.get("kind"), "node": args.get("node"), "reason": args.get("reason", ""),
            "minutes": args.get("minutes", 15), "payload": args.get("payload"),
            "session_id": caller.session_id})
        return {"ok": True, **out}

    async def approval_get(self, args: dict) -> dict:
        out = _unwrap(await self.relay.read("approval_get", {"request_id": args.get("request_id")}))
        return {"ok": True, **(out or {})}

    async def approval_wait(self, args: dict) -> dict:
        try:
            out = await self.relay.poll("approval_wait", {"request_id": args.get("request_id"),
                                                          "wait_s": args.get("wait_s", 55)})
        except Unreachable:
            return {**await self.approval_get(args), "alcor_down_since": self.relay.down_since(),
                    "outbox": self.relay.pending()}
        return {"ok": True, **out}

    async def approval_cancel(self, args: dict) -> dict:
        caller = await self._own(args)
        out = await self.relay.write("approval_cancel", {"request_id": args.get("request_id"),
                                                         "session_id": caller.session_id})
        return {"ok": True, **out}

    # -- orders (R97: alcor never acts on another node) ---------------------------------

    async def order_pull(self, args: dict) -> list:
        return await self.relay.poll("order_pull", {"node": args.get("node") or self.node,
                                                    "kinds": args.get("kinds"),
                                                    "wait_s": args.get("wait_s", 0)})

    async def order_done(self, args: dict) -> dict:
        out = await self.relay.write("order_done", {"order_id": args.get("order_id"),
                                                    "ok": bool(args.get("ok")),
                                                    "note": args.get("note", "")})
        return {"ok": True, **out}

    async def keys(self, args: dict) -> dict:
        return _unwrap(await self.relay.read("keys", {}))

    async def status(self, args: dict) -> dict:
        return {"ok": True, **self.relay.status()}

    VERBS = ("task_open", "task_attach", "task_join", "task_report", "task_session_end",
             "task_for_session", "task_sessions", "task_get", "task_list", "request_human",
             "hook_prompt", "approval_request", "approval_get", "approval_wait",
             "approval_cancel", "order_pull", "order_done", "keys", "status")

    async def call(self, verb: str, args: dict) -> Any:
        if verb not in self.VERBS:
            return {"ok": False, "error": f"unknown verb {verb!r}"}
        try:
            return await getattr(self, verb)(dict(args or {}))
        except (ValueError, Refused) as exc:
            return {"ok": False, "error": str(exc)}

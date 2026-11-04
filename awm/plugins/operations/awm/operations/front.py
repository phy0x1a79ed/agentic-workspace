"""The ``operations`` verbs a session calls on its own node (queue.md R145, fleet.md R106).

A verb names the calling session by the pid the gateway stamped on the call, which
reflection turns into the session's R9 id (sessions.md R9). A service with no session
passes ``session_id`` instead, except where only the session itself may act: an approval
request records the calling session, and only that session cancels it (fleet.md R97
item 1). Every write goes through the relay, so it waits in the outbox while alcor is
away.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from awm.gatewayclient.ids import normalise

from awm.operations.relay import Refused, Relay, Unreachable

log = logging.getLogger("awm.operations.front")

NEEDS_YOU_STATES = ("planned", "blocked", "review")
TITLE_CHARS = 80
HEADING = re.compile(r"^#+\s+(.+?)\s*#*\s*$", re.M)
CLAIM_PREFIX = re.compile(r"^\s*claim\s+task\s+", re.I)
NOTICE = ("This session's work is card {task_id} on Tony's Swarm board. Move it with operations "
          "task_report, and call request_human when you need Tony.")


def remind_s() -> float:
    """How long an urgent block waits before Tony gets one reminder."""
    return float(os.environ.get("AWM_OPERATIONS_REMIND_S", "3600"))

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
    clock: Callable[[], float] = field(default=time.time)

    # -- who is calling --------------------------------------------------------

    async def _caller(self, args: dict) -> Caller:
        pid = args.pop("_caller_pid", None)
        # An isolated session has no host pid: the gateway's isolated door stamps its R9 id.
        isolated = args.pop("_caller_session_id", None)
        if isolated:
            return Caller(normalise(isolated))
        rec = await self.session_of(pid) if isinstance(pid, int) else None
        if not rec:
            return Caller(None)
        caller = Caller(normalise(rec["session_id"]), rec.get("job"), rec.get("name"))
        if caller.job:
            self.relay.mark(f"job:{caller.job}", caller.session_id)
        return caller

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
            message = (f"**{caller.name or task_id}** on **{self.node}** needs you.\n{text}\n"
                       f"Card {task_id}. Attach: `claude attach "
                       f"{caller.job or caller.name or '<job>'}` on {self.node}")
            out["notified"] = await self.dm(message)
            self.relay.mark(f"remind:{task_id}", json.dumps(
                {"due": self.clock() + remind_s(), "text": message}))
        return out

    async def remind_due(self) -> int:
        """Send the one reminder of each urgent block that is still blocked. Returns how many went."""
        sent = 0
        for key, value in self.relay.marks("remind:").items():
            task_id = key.removeprefix("remind:")
            try:
                due = json.loads(value)
            except ValueError:
                self.relay.mark(key, clear=True)
                continue
            if self.clock() < float(due.get("due") or 0):
                continue
            try:
                card = _unwrap(await self.relay.read("task_get", {"task_id": task_id}))
            except Refused:
                card = None
            if isinstance(card, dict) and card.get("section") == "needs_you" \
                    and card.get("status") == "blocked":
                if not await self.dm("Still waiting: " + str(due.get("text") or task_id)):
                    continue  # try again on the next pass
                sent += 1
            elif card is None and self.relay.down_since() is not None:
                continue  # alcor is away: decide when it answers
            self.relay.mark(key, clear=True)
        return sent

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
            return {"ok": True, "moved": False, "blocked": False}
        await self.task_report({"task_id": task_id, "session_id": caller.session_id,
                                "state": "working"})
        self.relay.mark(f"remind:{task_id}", clear=True)
        return {"ok": True, "moved": True, "task_id": task_id, "blocked": False}

    # -- the plan, todo and clear hooks, and a session's own card -------------------

    async def _card_or_open(self, session: str, caller: Caller, title: str) -> tuple[str, bool]:
        task_id = await self._task_of(session)
        if task_id:
            return task_id, False
        out = await self.relay.write("task_open", {
            "title": _title(title) or caller.name or f"Work on {self.node}",
            "session_id": session, "name": caller.name})
        return out["task_id"], True

    def _opened(self, out: dict, task_id: str, opened: bool) -> dict:
        out = {**out, "task_id": task_id, "opened": opened}
        if opened:
            out["notice"] = NOTICE.format(task_id=task_id)
        return out

    async def hook_plan(self, args: dict) -> dict:
        """A plan was written: the card shows it and waits in Needs you as planned."""
        session, caller = await self._session(args, required=True)
        text = str(args.get("plan_text") or "")
        heading = HEADING.search(text)
        task_id, opened = await self._card_or_open(session, caller,
                                                   heading.group(1) if heading else "")
        await self.relay.write("task_detail", {"task_id": task_id, "plan": text})
        out = await self.task_report({"task_id": task_id, "session_id": session,
                                      "state": "planned"})
        return self._opened({**out, "blocked": True}, task_id, opened)

    async def hook_plan_approved(self, args: dict) -> dict:
        """The plan was approved: the card moves to Working."""
        session, caller = await self._session(args, required=True)
        text = str(args.get("plan_text") or "")
        heading = HEADING.search(text)
        task_id, opened = await self._card_or_open(session, caller,
                                                   heading.group(1) if heading else "")
        if text:
            await self.relay.write("task_detail", {"task_id": task_id, "plan": text})
        out = await self.task_report({"task_id": task_id, "session_id": session,
                                      "state": "working"})
        return self._opened({**out, "blocked": False}, task_id, opened)

    async def hook_todos(self, args: dict) -> dict:
        """The session's todo list changed. A session with no card is left alone."""
        items = args.get("items")
        if not isinstance(items, list):
            raise ValueError("items must be a list")
        session, _ = await self._session(args, required=True)
        task_id = await self._task_of(session)
        if not task_id:
            return {"ok": True, "tracked": False}
        await self.relay.write("task_detail", {"task_id": task_id, "todos": items})
        return {"ok": True, "tracked": True, "task_id": task_id}

    async def hook_session_clear(self, args: dict) -> dict:
        """A /clear gave the job a new session id. The old one leaves its card.

        The new id has no card, so the job's next plan opens one of its own. The old id is
        the last one this relay saw for the job, or the one the harness still reports.
        """
        pid = args.pop("_caller_pid", None)
        args.pop("_caller_session_id", None)
        rec = await self.session_of(pid) if isinstance(pid, int) else None
        if not rec or not rec.get("job"):
            return {"ok": True, "ended": None}
        new = normalise(str(args.get("new_session_id") or rec["session_id"]))
        seen = self.relay.mark(f"job:{rec['job']}")
        reported = normalise(rec["session_id"])
        old = next((s for s in (seen, reported) if s and s != new), None)
        self.relay.mark(f"job:{rec['job']}", new)
        task_id = await self._task_of(old)
        if not task_id:
            return {"ok": True, "ended": None}
        await self.relay.write("task_session_end", {"task_id": task_id, "session_id": old})
        self.relay.mark(f"needs:{old}", clear=True)
        return {"ok": True, "ended": old, "task_id": task_id}

    async def claim_task(self, args: dict) -> dict:
        """Take a card for the calling session, when Tony says ``claim task <id>``."""
        session, caller = await self._session(args, required=True)
        ref = CLAIM_PREFIX.sub("", str(args.get("task_id") or args.get("ref") or "")).split()
        if not ref:
            raise ValueError("task_id is required")
        out = await self.relay.write("task_claim", {"task_id": ref[0], "session_id": session,
                                                    "name": caller.name})
        self.relay.mark(f"needs:{session}", clear=True)
        return {"ok": True, **out}

    async def track_task(self, args: dict) -> dict:
        """The calling session's card, or a new one in Working."""
        session, caller = await self._session(args, required=True)
        task_id, opened = await self._card_or_open(session, caller, str(args.get("title") or ""))
        return self._opened({"ok": True}, task_id, opened)

    async def swarm_message_post(self, args: dict) -> dict:
        """A message on a card, signed with the caller's name and node."""
        session, caller = await self._session(args)
        out = await self.relay.write("task_message", {
            "task_id": args.get("card_id"), "body": str(args.get("body") or ""),
            "author": caller.name or session or "a service", "session_id": session})
        return {"ok": True, **out}

    async def swarm_messages(self, args: dict) -> dict:
        return {"ok": True, "card_id": args.get("card_id"),
                "messages": _unwrap(await self.relay.read(
                    "task_messages", {"task_id": args.get("card_id"),
                                      "after": int(args.get("after") or 0)}))}

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
             "hook_prompt", "hook_plan", "hook_plan_approved", "hook_todos",
             "hook_session_clear", "claim_task", "track_task", "swarm_message_post",
             "swarm_messages", "approval_request", "approval_get", "approval_wait",
             "approval_cancel", "order_pull", "order_done", "keys", "status")

    async def call(self, verb: str, args: dict) -> Any:
        if verb not in self.VERBS:
            return {"ok": False, "error": f"unknown verb {verb!r}"}
        try:
            return await getattr(self, verb)(dict(args or {}))
        except (ValueError, Refused) as exc:
            return {"ok": False, "error": str(exc)}

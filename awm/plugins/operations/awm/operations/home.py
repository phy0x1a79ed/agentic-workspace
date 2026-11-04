"""The home node's side of operations: the store and the approver behind one dispatch (fleet.md R106, R97).

Every relay reaches these verbs, its own node's included, through :meth:`Home.call`. A
relay on another node arrives through the singleton door (R98), which names the node it
came from. The verbs that only Tony's panel may cause, such as card actions and
decisions, are not here: they reach the store and the approver only from the panel.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from awm.operations.approver.core import Approver, ApproverError
from awm.operations.store import QueueError, Store


def _call(fn: Callable, positional: tuple[str, ...], args: dict, drop: tuple[str, ...] = ()) -> Any:
    args = {k: v for k, v in (args or {}).items() if k not in drop}
    pos = [args.pop(k) if k in args else None for k in positional]
    return fn(*pos, **args)


class Home:
    def __init__(self, store: Store, approver: Approver, writer: Any = None) -> None:
        self.store, self.approver, self.writer = store, approver, writer
        s, a = store, approver
        self._sync: dict[str, Callable[[dict], Any]] = {
            "task_open": lambda x: _call(s.task_open, ("title",), x),
            "task_attach": lambda x: _call(s.task_attach, ("task_id", "session_id"), x),
            "task_join": lambda x: _call(s.task_join, ("task_id", "session_id"), x),
            "task_report": lambda x: _call(s.task_report, ("task_id", "session_id", "state"), x),
            "task_session_end": lambda x: _call(s.task_session_end, ("task_id", "session_id"), x),
            "task_for_session": lambda x: s.task_for_session(x["session_id"]),
            "task_sessions": lambda x: s.task_sessions(x["task_id"]),
            "task_get": lambda x: s.task_get(x["task_id"]),
            "task_list": lambda x: _call(s.task_list, (), x),
            "approval_get": lambda x: a.approval_get(x["request_id"]),
            "order_done": lambda x: a.order_done(x["order_id"], bool(x["ok"]), x.get("note", "")),
            "keys": lambda x: {"grant_pub": a.keys.grant_pub_b64u(),
                               "ack_pub": a.keys.ack_pub_spki_b64u(),
                               "ssh_user_ca": a.keys.ssh_ca_pub()},
        }

    VERBS = ("task_open", "task_attach", "task_join", "task_report", "task_session_end",
             "task_for_session", "task_sessions", "task_get", "task_list", "approval_request",
             "approval_get", "approval_wait", "approval_cancel", "order_pull", "order_done", "keys",
             "ops_post")

    async def call(self, verb: str, args: dict[str, Any], node: str | None = None) -> Any:
        """One verb from a relay. Raises ValueError for a refusal the relay must not replay."""
        args = {k: v for k, v in (args or {}).items() if k != "op_id" or verb.startswith("task_")}
        if node and verb in ("task_open", "task_attach", "task_join"):
            args["node"] = node  # the door's key names the node: a node speaks only for itself
        try:
            if verb in self._sync:
                return await asyncio.to_thread(self._sync[verb], args)
            a = self.approver
            if verb == "approval_request":
                if args.get("request_id"):  # a replay after a lost reply
                    try:
                        seen = a.approval_get(args["request_id"])
                    except ApproverError:
                        seen = None
                    if seen and seen["requester_session"] == args.get("session_id"):
                        return {k: seen[k] for k in ("request_id", "task_id", "expires")}
                return await a.approval_request(
                    args["kind"], args.get("node"), args.get("reason", ""),
                    args.get("minutes", 15), args.get("payload"), args.get("session_id"),
                    args.get("request_id"))
            if verb == "approval_wait":
                return await a.approval_wait(args["request_id"], float(args.get("wait_s", 55)))
            if verb == "approval_cancel":
                seen = a.approval_get(args["request_id"])
                if seen["status"] == "cancelled" and seen["requester_session"] == args.get("session_id"):
                    return seen  # a replay after a lost reply
                return await a.approval_cancel(args["request_id"], args.get("session_id"))
            if verb == "ops_post":
                if self.writer is None:
                    raise ValueError("no board writer on this node")
                note = await asyncio.to_thread(self.writer.post_ops, args["kind"], args["node"],
                                               args["title"], args["body"])
                return {"note_id": note}
            if verb == "order_pull":
                if node and args.setdefault("node", node) != node:
                    raise ValueError(f"{node} pulls only its own orders, not {args['node']}'s")
                return await a.order_pull(args["node"], args.get("kinds"),
                                          float(args.get("wait_s", 0)))
        except (QueueError, KeyError, TypeError) as exc:
            raise ValueError(str(exc) or repr(exc)) from None
        except ApproverError as exc:
            raise ValueError(f"{exc.code}: {exc}") from None
        raise ValueError(f"{verb} is not a home verb")

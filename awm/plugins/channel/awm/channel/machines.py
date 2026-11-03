"""The tether tool, as the machine kind of channel sees it.

A machine channel is one tether session. The channel plugin never talks to the
tether daemon itself: it calls the tether tool through the gateway, so the
tether keeps one record of who minted which session. A call made for the
holder carries the holder's pid, and the gateway stamps it as the tether's
``_caller_pid``, so the tether records the holder as the session's operator and
the holder can still use the shell verbs on it. A call the service makes on its
own, following or pruning, carries no pid and names the session with ``adopt``.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from awm import gatewayclient

Call = Callable[..., Awaitable[Any]]

#: How long one ``drain`` waits for an event. The tether caps it at 90 s.
WAIT_S = 60


class MachineGone(Exception):
    """The tether no longer has the session."""


class MachineError(Exception):
    """The tether refused, or could not be reached. The message says why."""


class Machines:
    def __init__(self, call: Call = gatewayclient.call) -> None:
        self._call = call

    async def invite(self, os_name: str, caller_pid: int | None) -> dict:
        """A new session: its slot, its session id and the owner's command."""
        answer = await self._ask("invite", {"os": os_name}, caller_pid, timeout=90.0)
        if not isinstance(answer.get("slot"), int):
            raise MachineError("the tether minted no usable slot")
        return answer

    async def send(self, slot: int, text: str, caller_pid: int | None) -> dict:
        return await self._ask("send", self._aimed(slot, caller_pid, text=text),
                               caller_pid)

    async def cut(self, slot: int, reason: str, caller_pid: int | None = None) -> None:
        try:
            await self._ask("cut", self._aimed(slot, caller_pid, reason=reason), caller_pid)
        except MachineGone:
            pass

    async def drain(self, slot: int, cursor: int) -> dict:
        """The session's owner and phase events after ``cursor``, held up to ``WAIT_S``."""
        args = self._aimed(slot, None, cursor=cursor, types="owner.,operator.,session.phase",
                           wait=WAIT_S)
        return await self._ask("drain", args, None, timeout=WAIT_S + 30.0)

    @staticmethod
    def _aimed(slot: int, caller_pid: int | None, **fields: Any) -> dict:
        args: dict[str, Any] = {"code": str(slot), **fields}
        if caller_pid is None:
            args["adopt"] = True
        return args

    async def _ask(self, verb: str, args: dict, caller_pid: int | None, *,
                   timeout: float = 60.0) -> dict:
        try:
            answer = await self._call("tether", verb, args, caller_pid=caller_pid,
                                      timeout=timeout)
        except gatewayclient.GatewayCallError as exc:
            raise MachineError(f"the tether tool refused {verb}: {exc}") from exc
        if not isinstance(answer, dict):
            raise MachineError(f"the tether tool answered {verb} with {answer!r}")
        if answer.get("ok") is False or "error" in answer:
            error = str(answer.get("error") or "no reason given")
            if "names no session" in error or "no live session" in error:
                raise MachineGone(error)
            raise MachineError(error)
        return answer

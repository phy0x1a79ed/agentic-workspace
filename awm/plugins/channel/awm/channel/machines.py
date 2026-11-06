"""The machine kind of channel: one tether session, driven in this process.

The channel plugin supervises this host's tether binary and reads its event
stream itself (``awm.tether``). No other tool reaches the operator
daemon, so the channel store is the one record of who holds which session, and
a session answers only to the channel that minted it.

A slot is not an identity: the daemon hands a number out again once its session
ends. Every verb therefore names the session id the stream gave the mint, and
refuses when the slot now carries a later session.

What the daemon reports splits in two (``channels.md`` R110 item 5). What the
two people say is conversation: the channel follows it and pushes the owner's
lines. What ran and what it printed is a tool log: ``drain`` reads it, and it is
never pushed and never written to the channel transcript.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, Awaitable, Callable

from awm.tether import control, daemon, paths, remote_os, stream
from awm.tether.report import release_report

log = logging.getLogger("awm.channel.machines")

#: The event kinds that are conversation and session state. Everything else the
#: daemon reports about a session is its tool log.
TALK = ("owner.", "operator.", "session.phase")
LOG = ("task.",)

#: How long `invite` waits for its own mint to come round on the stream, so it
#: can name the session id.
MINT_WAIT_S = 5.0

#: The most a `drain` waits for something to arrive.
MAX_WAIT_S = 90.0

#: The daemon's own words for a session that is not there any more.
_GONE = ("names no session", "there is no session", "has ended", "no session is open")

Call = Callable[..., Awaitable[dict]]


class MachineGone(Exception):
    """The tether no longer has the session."""


class MachineError(Exception):
    """The tether refused, or could not be reached. The message says why."""


class Machines:
    def __init__(self, journal: stream.Journal | None = None,
                 mark: stream.Mark | None = None,
                 logs: stream.SessionLogs | None = None,
                 call: Call = control.call) -> None:
        self.journal = journal or stream.Journal()
        self.mark = mark or stream.Mark()
        self.logs = logs or stream.SessionLogs(mark=self.mark)
        self._call = call
        self._child: daemon.Adopted | None = None

    @property
    def child(self) -> daemon.Adopted:
        if self._child is None:
            self._child = daemon.for_role()
        return self._child

    # -- the session's lifecycle --------------------------------------------

    async def invite(self, os_name: str | None) -> dict:
        """Mint a session for one declared kind of computer.

        Refused before reaching the daemon when the OS is missing or unknown: a
        session minted on a guess is the failure this exists to prevent.
        """
        refused = remote_os.refusal(os_name)
        if refused:
            raise MachineError(refused)
        floor = self.journal.head()["next"]
        answer = await self._ask("invite", {}, timeout=60.0)
        slot = answer.get("slot")
        if not isinstance(slot, int):
            raise MachineError("the tether minted no usable slot")
        self.mark.declared[slot] = {"os": os_name,
                                    "word": str(answer.get("code") or "").split(" ")[0]}
        sid = self.mark.claim(slot, floor) or await self._await_mint(slot)
        line = remote_os.launcher_line(os_name, answer.get("command") or "")
        return {
            **answer,
            "session": sid,
            "command": line,
            "read_out": f"{remote_os.CHOICES[os_name]['open']}, then answer the prompt "
                        f"it shows you: {line}",
            "os": remote_os.describe(os_name),
        }

    async def _await_mint(self, slot: int) -> str | None:
        if not self.journal.head()["connected"]:
            return None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + MINT_WAIT_S
        while slot in self.mark.claims:
            left = deadline - loop.time()
            if left <= 0:
                return None
            await self.journal.wait(left)
        return self.mark.current(slot)

    def session_of(self, slot: int) -> str | None:
        return self.mark.current(slot)

    async def cut(self, slot: int, sid: str, reason: str) -> None:
        try:
            await self._aimed("cut", slot, sid, reason=reason)
        except MachineGone:
            pass

    # -- what the operator does on the machine ------------------------------

    async def say(self, slot: int, sid: str, text: str) -> dict:
        return await self._aimed("send", slot, sid, text=text)

    async def run(self, slot: int, sid: str, command: str,
                  limit_s: int | None = None) -> dict:
        fields: dict[str, Any] = {"command": command}
        if limit_s is not None:
            fields["limit_s"] = int(limit_s)
        return await self._aimed("run", slot, sid, **fields)

    async def terminal(self, slot: int, sid: str, command: str | None,
                       cols: int | None, rows: int | None) -> dict:
        fields = {k: v for k, v in (("command", command), ("cols", cols), ("rows", rows))
                  if v not in (None, "")}
        return await self._aimed("shell", slot, sid, **fields)

    async def keys(self, slot: int, sid: str, task: int, raw: bytes) -> dict:
        return await self._aimed("keys", slot, sid, task=int(task),
                                 data=base64.b64encode(raw).decode("ascii"))

    async def stop_task(self, slot: int, sid: str, task: int) -> dict:
        return await self._aimed("close", slot, sid, task=int(task))

    async def tasks(self, slot: int, sid: str) -> dict:
        return await self._aimed("tasks", slot, sid)

    async def last_task(self, slot: int, sid: str) -> int | None:
        """The newest task still open in the session, for keys with no task."""
        answer = await self.tasks(slot, sid)
        open_tasks = [t.get("task") for t in answer.get("tasks") or []
                      if isinstance(t, dict) and isinstance(t.get("task"), int)]
        return max(open_tasks) if open_tasks else None

    def os_of(self, slot: int, reported: str | None = None) -> dict:
        declared = self.mark.declared.get(slot, {}).get("os")
        return remote_os.describe(declared, reported)

    async def describe_os(self, slot: int, *, brief: bool = False) -> dict:
        """What the session's machine was declared as, beside what its client reports."""
        reported = None
        try:
            report = await self._ask("status", {}, timeout=30.0)
        except (MachineError, MachineGone):
            report = {}
        for session in report.get("sessions") or []:
            if session.get("slot") == slot:
                reported = (session.get("owner") or {}).get("os")
        about = self.os_of(slot, reported)
        if brief:
            about = {k: about[k] for k in ("declared", "reported", "shell", "mismatch",
                                           "warning") if k in about}
        return about

    # -- the stream ---------------------------------------------------------

    def epoch(self) -> int | None:
        return self.journal.head()["epoch"]

    async def events(self, sid: str, cursor: int, *, types: tuple[str, ...],
                     wait: float = 0.0, until: int | None = None,
                     task: int | None = None, limit: int = 500) -> dict:
        """The session's events of ``types`` from ``cursor``, held up to ``wait``.

        The cursor belongs to one daemon epoch. The caller compares the answer's
        ``epoch`` with the one it saved and starts again from 0 when they differ.
        """
        sessions = {sid}
        hold = max(0.0, min(float(wait or 0), MAX_WAIT_S))
        if hold:
            found, _, _ = self.journal.since(cursor, limit=limit, task=task, types=types,
                                             sessions=sessions)
            if not found:
                await self.journal.wait(hold, until=until, sessions=sessions)
        events, nxt, gap = self.journal.since(cursor, limit=limit, task=task, types=types,
                                              sessions=sessions)
        head = self.journal.head()
        return {"epoch": head["epoch"], "cursor": nxt, "more": nxt < head["next"],
                "gap": gap, "events": events}

    async def watch_forever(self) -> None:
        """Hold the daemon's event stream open for the life of the plugin."""
        await stream.watch_forever(self.journal, self.logs, _no_emit, self.mark)

    async def supervise_forever(self) -> None:
        """Start this host's tether binary, and keep starting it. Never returns."""
        while True:
            try:
                await asyncio.to_thread(self.child.reconcile)
            except Exception:  # noqa: BLE001 — a bad tick must not end the loop
                log.exception("could not reconcile the %s child", paths.ROLE)
            await asyncio.sleep(daemon.TICK_S)

    async def report(self) -> dict:
        """This host's tether binary: role, release, child and stream."""
        return {
            "role": paths.ROLE,
            "child": await asyncio.to_thread(self.child.snapshot),
            "release": release_report(),
            "stream": self.journal.head(),
            "log": str(paths.DAEMON_LOG),
        }

    def shutdown(self) -> None:
        self.logs.close()
        self.mark.save()
        if self._child is not None:
            self._child.shutdown()

    # -- internals ----------------------------------------------------------

    async def _aimed(self, verb: str, slot: int, sid: str, **fields: Any) -> dict:
        current = self.mark.current(slot)
        if current is not None and current != sid:
            raise MachineGone(f"slot {slot} now carries a later session")
        return await self._ask(verb, {"code": str(slot), **fields})

    async def _ask(self, verb: str, args: dict, *, timeout: float = 60.0) -> dict:
        if paths.ROLE == paths.RELAY:
            raise MachineError("this host is the relay: it carries tether sessions and "
                               "mints none. Open a machine channel on the operator's node.")
        try:
            answer = await self._call(verb, args, timeout=timeout)
        except control.DaemonUnavailable as exc:
            raise MachineError(f"no tether daemon is answering on this host: {exc}. "
                               f"Its log is {paths.DAEMON_LOG}.") from exc
        if not isinstance(answer, dict):
            raise MachineError(f"the tether daemon answered {verb} with {answer!r}")
        if answer.get("ok") is False or "error" in answer:
            error = str(answer.get("error") or "no reason given")
            if any(marker in error for marker in _GONE):
                raise MachineGone(error)
            raise MachineError(error)
        return answer


async def _no_emit(topic: str, payload: dict) -> None:
    """The channel pushes what matters itself, so nothing is announced."""


def decoded(events: list[dict], raw: bool = False) -> list[dict]:
    """Join a task's output chunks and decode them to text.

    Output travels as bytes, because a terminal's stream is escape sequences and
    a command's can split a character across a chunk boundary. Consecutive
    chunks of one task and stream are joined first, so a split character is
    whole again. Each chunk is decoded on its own, because each carries its own
    base64 padding.
    """
    out: list[dict] = []
    for event in events:
        if event.get("type") != "task.output":
            out.append(dict(event))
            continue
        last = out[-1] if out else None
        if (last is not None and last.get("type") == "task.output"
                and last.get("task") == event.get("task")
                and last.get("stream") == event.get("stream")):
            last["_chunks"].append(event.get("data") or "")
            last["bytes"] = last.get("bytes", 0) + event.get("bytes", 0)
            continue
        joined = dict(event)
        joined["_chunks"] = [event.get("data") or ""]
        out.append(joined)
    for event in out:
        chunks = event.pop("_chunks", None)
        if chunks is None:
            continue
        blob = b""
        for chunk in chunks:
            try:
                blob += base64.b64decode(chunk)
            except Exception:  # noqa: BLE001 — one bad chunk is not the whole stream
                continue
        if raw:
            event["data"] = base64.b64encode(blob).decode("ascii")
            continue
        event.pop("data", None)
        event["text"] = blob.decode("utf-8", errors="replace")
    return out

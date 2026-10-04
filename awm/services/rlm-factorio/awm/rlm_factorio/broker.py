"""Realm-side client for the in-container RCON broker (``appliance/rcon_broker.py``).

One ``docker exec`` process per appliance container carries every game call,
each tagged with a connection key so the broker gives every seat its own engine
socket. The broker source is read from this tree at each start, so a broker
change ships with a realm restart and never needs an image rebuild.

The process holds the read end of our stdin pipe, so it exits when this process
does, however this process dies.
"""

from __future__ import annotations

import itertools
import json
import logging
import subprocess
import threading
from pathlib import Path

from awm.rlm_factorio.appliance import CONTAINER, ApplianceError

log = logging.getLogger("awm.rlm_factorio.broker")

BROKER_SRC = Path(__file__).resolve().parents[2] / "appliance" / "rcon_broker.py"


class BrokerError(ApplianceError):
    """A game call that did not complete. ``retryable`` means it never ran."""

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class _Slot:
    __slots__ = ("done", "reply")

    def __init__(self):
        self.done = threading.Event()
        self.reply: dict | None = None


class Broker:
    """Many concurrent callers, one broker process, replies matched by id."""

    def __init__(self, container: str):
        self.container = container
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._pending: dict[int, _Slot] = {}
        self._ids = itertools.count(1)

    def _command(self, src: str) -> list[str]:
        return ["docker", "exec", "-i", self.container, "python3", "-u", "-c", src]

    def _start(self) -> subprocess.Popen:
        src = BROKER_SRC.read_text(encoding="utf-8")
        proc = subprocess.Popen(
            self._command(src),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0)
        threading.Thread(target=self._read, args=(proc,), daemon=True,
                         name=f"broker-read-{self.container}").start()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True,
                         name=f"broker-err-{self.container}").start()
        log.info("broker started in %s (pid %d)", self.container, proc.pid)
        return proc

    def _read(self, proc: subprocess.Popen) -> None:
        for raw in proc.stdout:
            try:
                reply = json.loads(raw)
            except ValueError:
                log.warning("broker %s: unparseable line %r", self.container, raw[:200])
                continue
            with self._lock:
                slot = self._pending.pop(reply.get("id"), None)
            if slot is not None:
                slot.reply = reply
                slot.done.set()
        proc.wait()
        log.warning("broker in %s exited (code %s)", self.container, proc.returncode)
        with self._lock:
            if self._proc is proc:
                self._proc = None
            lost, self._pending = self._pending, {}
        for slot in lost.values():
            slot.reply = {"ok": False, "retryable": True,
                          "error": "broker exited before replying; retry"}
            slot.done.set()

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        for raw in proc.stderr:
            log.warning("broker %s: %s", self.container,
                        raw.decode("utf-8", "replace").rstrip())

    def _send(self, msg: dict, timeout: float) -> dict:
        slot = _Slot()
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._proc = self._start()
            rid = next(self._ids)
            self._pending[rid] = slot
            try:
                self._proc.stdin.write(
                    (json.dumps({**msg, "id": rid}) + "\n").encode("utf-8"))
            except OSError as exc:
                self._pending.pop(rid, None)
                raise BrokerError(f"broker unreachable ({exc}); retry",
                                  retryable=True) from exc
        if not slot.done.wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise BrokerError(
                f"no reply from the engine within {timeout:.0f}s; the command "
                "may still run, so check its effect before retrying",
                retryable=False)
        reply = slot.reply or {}
        if not reply.get("ok"):
            raise BrokerError(str(reply.get("error") or "broker call failed"),
                              retryable=bool(reply.get("retryable")))
        return reply

    def lua(self, conn: str, code: str, *, timeout: float) -> dict:
        """Run ``code`` on ``conn``'s socket. Returns {output, cost_ms}."""
        reply = self._send({"op": "lua", "conn": conn, "code": code}, timeout)
        return {"output": reply.get("output") or "", "cost_ms": reply.get("cost_ms")}

    def close_conn(self, conn: str, *, timeout: float = 15.0) -> None:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
        if running:
            self._send({"op": "close", "conn": conn}, timeout)

    def stop(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.stdin.close()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()


_BROKERS: dict[str, Broker] = {}
_BROKERS_LOCK = threading.Lock()


def for_row(row: dict) -> Broker:
    """The broker for a session's appliance container, created on first use."""
    container = row.get("container_name") or CONTAINER
    with _BROKERS_LOCK:
        broker = _BROKERS.get(container)
        if broker is None:
            broker = _BROKERS[container] = Broker(container)
        return broker


def stop_all() -> None:
    with _BROKERS_LOCK:
        brokers = list(_BROKERS.values())
    for broker in brokers:
        broker.stop()

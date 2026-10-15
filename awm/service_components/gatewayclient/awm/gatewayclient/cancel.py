"""Cancellation for one inbound gateway call.

The adapter binds a ``CancelToken`` for every call and trips it when the hub
sends a ``cancel`` frame. A sync handler runs in a worker thread that cannot be
interrupted, so cancellation reaches it through the subprocesses it starts:
``run`` puts each child in its own process group, kills the group when the
token trips, and raises ``Cancelled`` in the handler thread.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import signal
import subprocess
import threading
from typing import Iterator

_KILL_GRACE_S = 3.0


class Cancelled(Exception):
    """The caller cancelled the call this code runs for."""


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._procs: set[subprocess.Popen] = set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled("call cancelled by the caller")

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            procs = list(self._procs)
        for proc in procs:
            _kill_group(proc)

    def _track(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.add(proc)
        if self._event.is_set():
            _kill_group(proc)

    def _untrack(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.discard(proc)


_current: contextvars.ContextVar[CancelToken | None] = contextvars.ContextVar(
    "awm_cancel_token", default=None)


def current() -> CancelToken | None:
    return _current.get()


def activate(token: CancelToken | None) -> None:
    """Bind ``token`` for the rest of the current task or context copy."""
    _current.set(token)


@contextlib.contextmanager
def bound(token: CancelToken | None) -> Iterator[CancelToken | None]:
    reset = _current.set(token)
    try:
        yield token
    finally:
        _current.reset(reset)


def shielded() -> contextlib.AbstractContextManager:
    """Run cleanup that must finish even though the call was cancelled."""
    return bound(None)


def _kill_group(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return

    def _escalate() -> None:
        try:
            proc.wait(_KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)

    threading.Thread(target=_escalate, daemon=True).start()


def run(argv: list[str], *, cwd: str | None = None, env: dict | None = None,
        timeout: float | None = None, text: bool = True,
        check: bool = False) -> subprocess.CompletedProcess:
    """``subprocess.run(capture_output=True)`` whose process group dies with the call."""
    token = current()
    if token is not None:
        token.check()
    proc = subprocess.Popen(argv, cwd=cwd, env=env, text=text,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    if token is not None:
        token._track(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        proc.communicate()
        raise
    finally:
        if token is not None:
            token._untrack(proc)
    if token is not None:
        token.check()
    if check and proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, argv, out, err)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)

"""Report plugin use the gateway cannot see (contract § Usage event, R132).

The gateway counts tool calls and the pages it serves by itself. Two kinds of
use reach it only when the plugin reports them:

* a background run, such as a sweep, a nightly job or a lab run:
  ``record_background`` or ``background_run``;
* use straight from another project's code: ``record_code``.

Every helper returns at once and never raises. A count must not slow or fail
the work it counts.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import logging
import os
import threading
import time
import urllib.request
from typing import Any, Iterator

log = logging.getLogger("awm.usage")

TIMEOUT_S = 2.0
_pending: set[threading.Thread] = set()
_tasks: set[asyncio.Future] = set()
_lock = threading.Lock()


def gateway_url() -> str:
    return (os.environ.get("AWM_HUB_URL")
            or f"http://127.0.0.1:{os.environ.get('AWM_PORT', '7819')}").rstrip("/")


def _send(event: dict[str, Any]) -> None:
    req = urllib.request.Request(
        f"{gateway_url()}/usage", data=json.dumps(event).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001
        log.debug("usage event for %s dropped: %s", event.get("plugin"), exc)
    finally:
        with _lock:
            _pending.discard(threading.current_thread())


def post(event: dict[str, Any]) -> threading.Thread:
    """Send ``event`` as ``POST /usage`` on a daemon thread, and return the thread."""
    t = threading.Thread(target=_send, args=(event,), name="awm-usage", daemon=True)
    with _lock:
        _pending.add(t)
    t.start()
    return t


@atexit.register
def _drain() -> None:
    """Give a short-lived script's last events their timeout before the process ends."""
    deadline = time.monotonic() + TIMEOUT_S
    with _lock:
        pending = list(_pending)
    for t in pending:
        t.join(max(0.0, deadline - time.monotonic()))


def _adapter():
    try:
        from awm.gatewayclient.adapter import ServiceAdapter
    except ImportError:
        return None
    return ServiceAdapter.current


def _plugin(adapter) -> str | None:
    return (os.environ.get("AWM_PLUGIN") or os.environ.get("AWM_SERVICE_NAME")
            or (adapter.name if adapter is not None else None))


def record_background(job: str, ok: bool = True, ms: float | None = None, *,
                      plugin: str | None = None) -> None:
    """Count one background run of ``job``.

    Inside a running service it goes as a frame on the service's control WS,
    from any thread. Elsewhere, or with a ``plugin`` that is not the running
    service, it goes as ``POST /usage`` under ``plugin``, ``AWM_PLUGIN`` or
    ``AWM_SERVICE_NAME``.
    """
    try:
        adapter = _adapter()
        loop = getattr(adapter, "loop", None)
        if plugin is not None and plugin != getattr(adapter, "name", None):
            post({"plugin": plugin, "surface": "background", "name": job, "ok": ok, "ms": ms})
            return
        if loop is not None and loop.is_running():
            async def frame() -> None:
                if not await adapter.send_usage(job, ok, ms):
                    _post_background(adapter, job, ok, ms)

            try:
                on_loop = asyncio.get_running_loop() is loop
            except RuntimeError:
                on_loop = False
            fut = loop.create_task(frame()) if on_loop else asyncio.run_coroutine_threadsafe(frame(), loop)
            _tasks.add(fut)
            fut.add_done_callback(_tasks.discard)
            return
        _post_background(adapter, job, ok, ms)
    except Exception as exc:  # noqa: BLE001
        log.debug("background usage for %s dropped: %s", job, exc)


def _post_background(adapter, job: str, ok: bool, ms: float | None) -> None:
    plugin = _plugin(adapter)
    if not plugin:
        log.debug("background usage for %s dropped: set AWM_PLUGIN", job)
        return
    post({"plugin": plugin, "surface": "background", "name": job, "ok": ok, "ms": ms})


def record_code(plugin: str, name: str) -> None:
    """Count one use of ``plugin`` straight from code, ``name`` saying which part."""
    try:
        post({"plugin": plugin, "surface": "code", "name": name})
    except Exception as exc:  # noqa: BLE001
        log.debug("code usage for %s dropped: %s", plugin, exc)


@contextlib.contextmanager
def background_run(job: str, *, plugin: str | None = None) -> Iterator[None]:
    """Time the block as one run of ``job``. A raised exception counts it failed and propagates."""
    start = time.monotonic()
    ok = False
    try:
        yield
        ok = True
    finally:
        record_background(job, ok, round((time.monotonic() - start) * 1000), plugin=plugin)

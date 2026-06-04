"""Backend-process supervision for ``kind="stripe"`` registrations.

A stripe registration bundles a static frontend bundle with a long-running
backend service the hub spawns at register time. This module handles the
process lifecycle that the existing ``kind="static"`` / ``kind="url"`` paths
don't need:

* Allocate a free port from a configurable pool (default 7900-7999, set
  via ``AWM_STRIPE_PORT_POOL=<lo>-<hi>``).
* Spawn the backend with ``AWM_SERVICE_PORT=<port>`` injected into env;
  any ``${AWM_SERVICE_PORT}`` substring in cmd args is substituted before
  exec so backends that take port via argv work without env reads.
* Poll the declared health endpoint until 200; flip ``backend_status``
  ``starting`` -> ``ready``. Time out at 30s -> ``down``.
* SIGTERM the process group on lease close, 5s grace, SIGKILL fallback,
  release the port.

No auto-restart in dev — failures stay loud so the developer notices.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from awm import config

if TYPE_CHECKING:
    from awm.services.hub.registry import ServiceRecord

log = logging.getLogger("awm.hub.supervisor")

_HEALTH_TIMEOUT_S = 30.0
_HEALTH_POLL_INTERVAL_S = 0.5
_TERMINATE_GRACE_S = 5.0


# ---------------------------------------------------------------------------
# Port pool
# ---------------------------------------------------------------------------

class PortPoolExhausted(Exception):
    """All ports in the configured pool are in use."""


class PortPool:
    """Allocate free ports in a configured range, one at a time.

    Allocation uses a probe-bind to confirm the port is free at the OS
    level; an ``in-use`` set prevents the same port being handed out twice
    between probe and the eventual backend bind. There is still a small
    OS-level race between allocate() returning and the backend binding —
    if the backend fails to bind, that surfaces as the health check
    timing out, which is visible enough for dev use.
    """

    def __init__(self, start: int, end: int) -> None:
        if end < start:
            raise ValueError(f"port pool range invalid: {start}-{end}")
        self._range = range(start, end + 1)
        self._used: set[int] = set()
        self._lock = asyncio.Lock()

    async def allocate(self) -> int:
        async with self._lock:
            for port in self._range:
                if port in self._used:
                    continue
                if _probe_free(port):
                    self._used.add(port)
                    return port
        raise PortPoolExhausted(
            f"no free port in {self._range.start}-{self._range.stop - 1}"
        )

    async def release(self, port: int) -> None:
        async with self._lock:
            self._used.discard(port)


def _probe_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        s.close()
    return True


def _default_pool() -> PortPool:
    raw = os.environ.get("AWM_STRIPE_PORT_POOL", "7900-7999")
    try:
        lo_s, hi_s = raw.split("-", 1)
        lo = int(lo_s)
        hi = int(hi_s)
    except (ValueError, AttributeError):
        log.warning("AWM_STRIPE_PORT_POOL=%r invalid, using 7900-7999", raw)
        lo, hi = 7900, 7999
    return PortPool(lo, hi)


_pool_singleton: PortPool | None = None


def get_port_pool() -> PortPool:
    global _pool_singleton
    if _pool_singleton is None:
        _pool_singleton = _default_pool()
    return _pool_singleton


# ---------------------------------------------------------------------------
# Supervised process
# ---------------------------------------------------------------------------

@dataclass
class SupervisedProcess:
    """One spawned backend, tracked by the hub.

    ``status`` mirrors the matching field on ``ServiceRecord`` so the
    middleware can decide whether to serve, queue, or 503 a backend
    request without touching the supervisor directly.
    """

    name: str
    port: int
    process: subprocess.Popen
    log_path: Path
    health_path: str
    status: str = "starting"   # "starting" | "ready" | "down"
    health_task: asyncio.Task | None = None
    log_handle: object | None = field(default=None, repr=False)


def _substitute_port(cmd: list[str], port: int) -> list[str]:
    """Replace any ``${AWM_SERVICE_PORT}`` substring in cmd args with
    the integer port. Done before exec so polyglot backends that take
    port via argv (e.g. ``["./bin/serve", "--port", "${AWM_SERVICE_PORT}"]``)
    don't need a shell. This is NOT general shell variable expansion —
    only the one named placeholder.
    """
    needle = "${AWM_SERVICE_PORT}"
    out: list[str] = []
    for arg in cmd:
        if needle in arg:
            arg = arg.replace(needle, str(port))
        out.append(arg)
    return out


def _stripe_log_path(service_id: str) -> Path:
    log_dir = config.AWM_DIR / "logs" / "stripes"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{service_id}.log"


async def spawn_backend(
    record: "ServiceRecord",
    cmd: list[str],
    env_extra: dict[str, str],
    cwd: str | None,
    health_path: str,
) -> SupervisedProcess:
    """Allocate a port, spawn the backend, kick off the health poll.

    Returns immediately after Popen — the health poll runs as a background
    task and flips ``record.backend_status`` to ``ready`` (or ``down`` on
    timeout). Caller is responsible for calling ``terminate`` when the
    lease closes.
    """
    pool = get_port_pool()
    port = await pool.allocate()

    env = os.environ.copy()
    env.update(env_extra)
    # Hub-injected vars win on collision so the contract is unambiguous.
    env["AWM_SERVICE_PORT"] = str(port)

    final_cmd = _substitute_port(cmd, port)
    log_path = _stripe_log_path(record.service_id)
    log_handle = open(log_path, "ab", buffering=0)

    try:
        proc = subprocess.Popen(
            final_cmd,
            env=env,
            cwd=cwd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        log_handle.close()
        await pool.release(port)
        log.error("spawn failed for stripe %s: %s", record.name, exc)
        raise

    sup = SupervisedProcess(
        name=record.name,
        port=port,
        process=proc,
        log_path=log_path,
        health_path=health_path,
        log_handle=log_handle,
    )

    sup.health_task = asyncio.create_task(_run_health_poll(sup, record))
    _register_live(sup)

    log.info(
        "spawned stripe backend %s pid=%d port=%d log=%s",
        record.name, proc.pid, port, log_path,
    )
    return sup


async def _run_health_poll(sup: SupervisedProcess, record: "ServiceRecord") -> None:
    """Poll the health endpoint until 200, timeout, or process exit."""
    url = f"http://127.0.0.1:{sup.port}{sup.health_path}"
    deadline = asyncio.get_event_loop().time() + _HEALTH_TIMEOUT_S
    async with httpx.AsyncClient(timeout=2.0) as client:
        while True:
            if sup.process.poll() is not None:
                sup.status = "down"
                record.backend_status = "down"
                log.warning(
                    "stripe backend %s exited before becoming healthy (code=%s)",
                    sup.name, sup.process.returncode,
                )
                return
            if asyncio.get_event_loop().time() > deadline:
                sup.status = "down"
                record.backend_status = "down"
                log.warning(
                    "stripe backend %s health check timed out after %ss",
                    sup.name, _HEALTH_TIMEOUT_S,
                )
                return
            try:
                resp = await client.get(url)
                if 200 <= resp.status_code < 300:
                    sup.status = "ready"
                    record.backend_status = "ready"
                    log.info("stripe backend %s healthy on :%d", sup.name, sup.port)
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(_HEALTH_POLL_INTERVAL_S)


async def terminate(sup: SupervisedProcess) -> None:
    """SIGTERM the process group, wait up to 5s, SIGKILL if still alive.

    Releases the port back to the pool and closes the log handle. Safe to
    call more than once.
    """
    if sup.health_task is not None and not sup.health_task.done():
        sup.health_task.cancel()
        try:
            await sup.health_task
        except (asyncio.CancelledError, Exception):
            pass

    if sup.process.poll() is None:
        try:
            os.killpg(os.getpgid(sup.process.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            log.warning("SIGTERM failed for stripe %s: %s", sup.name, exc)

        try:
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, sup.process.wait),
                timeout=_TERMINATE_GRACE_S,
            )
        except asyncio.TimeoutError:
            log.warning("stripe %s did not exit on SIGTERM, sending SIGKILL", sup.name)
            try:
                os.killpg(os.getpgid(sup.process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.get_event_loop().run_in_executor(None, sup.process.wait)

    sup.status = "down"
    handle = sup.log_handle
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass
        sup.log_handle = None
    await get_port_pool().release(sup.port)
    _unregister_live(sup)
    log.info("terminated stripe backend %s (port %d released)", sup.name, sup.port)


# ---------------------------------------------------------------------------
# Process tracking (test cleanup)
# ---------------------------------------------------------------------------
# Tracks every backend spawned by this process so that test fixtures
# (which run across multiple event loops within one Python run) can
# synchronously kill leaked subprocesses without needing the originating
# loop. Production hubs don't depend on this — eviction-via-lease drives
# the canonical teardown path.

_live_processes: list[SupervisedProcess] = []


def _register_live(sup: SupervisedProcess) -> None:
    _live_processes.append(sup)


def _unregister_live(sup: SupervisedProcess) -> None:
    try:
        _live_processes.remove(sup)
    except ValueError:
        pass


# Test-only reset; production hubs never call this.
def _reset_for_tests() -> None:
    """Kill any leaked supervised processes (synchronously, no event loop
    needed) and reap them so their listening sockets are released. Does
    NOT reset the port-pool singleton — across tests within one run
    the pool advances through the range, avoiding TIME_WAIT collisions
    on reused ports. Safe to call from any thread."""
    leftovers = list(_live_processes)
    _live_processes.clear()
    for sup in leftovers:
        if sup.process.poll() is None:
            try:
                os.killpg(os.getpgid(sup.process.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        try:
            sup.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            if sup.log_handle is not None:
                sup.log_handle.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Service supervision (kind="service") — PID journal + 10s reconnect window
# ---------------------------------------------------------------------------
#
# Where stripe supervision allocates a port and polls a health endpoint,
# service supervision drives a different lifecycle: the service is the
# one that opens a control WS back to the hub, the hub never allocates a
# port for it, and the canonical "alive" signal is the control WS being
# held. Service registrations are journaled to disk so a hub restart
# can match returning services against their last-seen PIDs.

import json as _json
import time as _time
from contextlib import suppress

_SERVICES_STATE_FILENAME = "services.json"
_RECONNECT_WINDOW_S = 10.0


def _services_journal_path() -> Path:
    state_dir = config.AWM_DIR / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / _SERVICES_STATE_FILENAME


def load_service_journal() -> dict[str, dict]:
    """Return the journaled service map ({name: {...}}). Empty dict on
    missing or corrupt file (logged at warning)."""
    path = _services_journal_path()
    if not path.is_file():
        return {}
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as exc:
        log.warning("could not parse %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def write_service_journal(state: dict[str, dict]) -> None:
    path = _services_journal_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(_json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def update_service_journal_entry(name: str, patch: dict) -> None:
    """Read-modify-write one service entry. Called on register, control-WS
    open, control-WS close. Not atomic across writers — one event loop
    owns the supervisor so concurrent updates from the hub itself can't
    race; external `awm packages list` reads are tolerant of partial
    writes (tmp-then-rename above)."""
    state = load_service_journal()
    entry = state.get(name, {})
    entry.update(patch)
    entry.setdefault("name", name)
    state[name] = entry
    write_service_journal(state)


def remove_service_journal_entry(name: str) -> None:
    state = load_service_journal()
    if state.pop(name, None) is not None:
        write_service_journal(state)


def spawn_service(name: str, start_cmd: list[str], cwd: str,
                  env_extra: dict[str, str]) -> int:
    """Launch ``start_cmd`` for a service. Returns the spawned PID.

    The hub injects ``AWM_HUB_URL``, ``AWM_HUB_TOKEN``, ``AWM_SERVICE_NAME``
    in env (caller is expected to set them in ``env_extra``). stdout +
    stderr land in ``<AWM_DIR>/logs/services/<name>.log``.
    """
    log_dir = config.AWM_DIR / "logs" / "services"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{name}.log"
    handle = open(log_path, "ab", buffering=0)
    env = os.environ.copy()
    env.update(env_extra)
    try:
        proc = subprocess.Popen(
            start_cmd,
            env=env,
            cwd=cwd or None,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except (OSError, ValueError):
        handle.close()
        raise
    log.info("spawned service %s pid=%d log=%s", name, proc.pid, log_path)
    return proc.pid


def kill_pid_group(pid: int, *, grace_s: float = 5.0) -> None:
    """SIGTERM the process group of ``pid``, wait ``grace_s``, SIGKILL
    if still alive. Safe to call on a stale PID — ProcessLookupError is
    swallowed (the corpse is already reaped)."""
    if pid <= 0:
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    except OSError as exc:
        log.warning("SIGTERM failed for pid=%d: %s", pid, exc)
        return
    deadline = _time.monotonic() + grace_s
    while _time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        _time.sleep(0.1)
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(pid), signal.SIGKILL)


async def reconcile_journaled_services() -> None:
    """Boot-time reconcile loop.

    For each journaled service, give it a 10s window to reopen its
    control WS. If it doesn't, SIGTERM the last-known PID and respawn
    from start_cmd. Called from the FastAPI startup event after the
    hub control-plane routes are mounted.
    """
    from awm.services.hub.registry import get_registry
    journal = load_service_journal()
    if not journal:
        return
    registry = get_registry()
    # Re-create the registry record so the control WS handler accepts
    # the reconnect. The service_id needs to match the journal so the
    # service's reconnect targets the right URL — we store it in the
    # journal.
    deadline = asyncio.get_event_loop().time() + _RECONNECT_WINDOW_S
    for name, entry in list(journal.items()):
        if not isinstance(entry, dict):
            continue
        sid = entry.get("service_id")
        prefix = entry.get("prefix") or f"/svc/{name}"
        try:
            rec = await registry.register_service(
                name, prefix,
                pid=entry.get("last_pid"),
                start_cmd=list(entry.get("start_cmd") or []),
                cwd=entry.get("cwd") or "",
            )
            # Restore the journaled service_id so the service hits the
            # same control URL it had before the restart.
            if sid:
                old = rec.service_id
                rec.service_id = sid
                # registry.get_by_id reads service_id off the record, so
                # the rebind is sufficient. No name map update needed.
                log.debug("rebound service %s id %s->%s", name, old, sid)
        except Exception as exc:
            log.warning("could not re-register journaled service %s: %s",
                        name, exc)
            continue

    # Park; let reconnects flow. After the window, respawn the silent ones.
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.5)

    from awm.services.hub import rpc as _rpc
    for name in list(journal.keys()):
        entry = journal.get(name) or {}
        sid = entry.get("service_id")
        ch = _rpc.get_control(sid) if sid else None
        if ch is not None and ch.ready.is_set():
            log.info("service %s reconnected within window", name)
            continue
        last_pid = entry.get("last_pid")
        start_cmd = entry.get("start_cmd") or []
        if not start_cmd:
            log.warning(
                "service %s did not reconnect and has no start_cmd; leaving",
                name,
            )
            continue
        if last_pid:
            log.info("service %s silent; killing stale pid=%d", name, last_pid)
            await asyncio.get_event_loop().run_in_executor(
                None, kill_pid_group, last_pid,
            )
        try:
            new_pid = spawn_service(
                name,
                start_cmd,
                entry.get("cwd") or "",
                {
                    "AWM_HUB_URL": entry.get("hub_url", ""),
                    "AWM_HUB_TOKEN": entry.get("hub_token", ""),
                    "AWM_SERVICE_NAME": name,
                    "AWM_SERVICE_ID": sid or "",
                },
            )
            update_service_journal_entry(name, {"last_pid": new_pid})
        except (OSError, ValueError) as exc:
            log.error("respawn failed for service %s: %s", name, exc)

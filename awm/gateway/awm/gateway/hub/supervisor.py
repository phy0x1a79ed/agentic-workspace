"""Service supervision for ``kind="service"`` registrations.

Services open a persistent control WS back to the hub. The hub never
allocates a port for them and never polls a health endpoint — the
canonical "alive" signal is the control WS being held. Service
registrations are journaled to disk so a hub restart can match
returning services against their last-seen PIDs and respawn the
silent ones from ``start_cmd``.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import signal
import subprocess
import time as _time
from contextlib import suppress
from pathlib import Path

from awm import config

log = logging.getLogger("awm.hub.supervisor")


# ---------------------------------------------------------------------------
# Service supervision — PID journal + 10s reconnect window
# ---------------------------------------------------------------------------

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
    from awm.gateway.hub.registry import get_registry
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

    from awm.gateway.hub import rpc as _rpc
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
            # The gateway is the hub, so inject its own loopback URL rather than
            # trusting a journal field — entries written by an earlier manual
            # launch may lack ``hub_url`` entirely, which would respawn the
            # service with an empty AWM_HUB_URL and it would die on boot.
            hub_url = entry.get("hub_url") or f"http://{config.HOST}:{config.PORT}/"
            new_pid = spawn_service(
                name,
                start_cmd,
                entry.get("cwd") or "",
                {
                    "AWM_HUB_URL": hub_url,
                    "AWM_HUB_TOKEN": entry.get("hub_token") or os.environ.get("AWM_HUB_TOKEN", ""),
                    "AWM_SERVICE_NAME": name,
                    "AWM_SERVICE_ID": sid or "",
                },
            )
            update_service_journal_entry(name, {"last_pid": new_pid})
        except (OSError, ValueError) as exc:
            log.error("respawn failed for service %s: %s", name, exc)

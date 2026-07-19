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
# How often the runtime self-heal sweep looks for wedged services. Comfortably
# longer than the disconnect watchdog's reconnect window so the two don't race
# to respawn the same crash.
_SELF_HEAL_INTERVAL_S = 45.0

# Set true while the gateway is tearing down (graceful lifespan shutdown). The
# crash-respawn watchdog checks this so it does not fight the teardown by
# resurrecting services the gateway is deliberately stopping. Set as early as
# possible (a SIGTERM handler in server.lifespan) so it is true before the
# control WSs start closing.
_shutting_down = False


def set_shutting_down(value: bool) -> None:
    global _shutting_down
    _shutting_down = bool(value)


def is_shutting_down() -> bool:
    return _shutting_down


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
    race; external `awm services list` reads are tolerant of partial
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

    The hub injects ``AWM_HUB_URL``, ``AWM_SERVICE_NAME`` (and ``AWM_SERVICE_ID``
    on respawn) in env (caller is expected to set them in ``env_extra``). No
    auth — the registration handshake carries no token. stdout + stderr land in
    ``<AWM_DIR>/logs/services/<name>.log``.
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


def _try_reap(pid: int) -> bool:
    """Non-blocking ``waitpid`` to clear our child ``pid``'s zombie once it has
    exited. Returns True only when we actually reaped *this* pid.

    Services are spawned via ``subprocess.Popen`` whose handle we discard
    (``spawn_service`` returns only the pid), so nothing else ever waits on
    them — when we kill one it lingers as a ``<defunct>`` zombie under the
    gateway until the gateway itself exits. Reaping here keeps the process
    table clean. ``ChildProcessError`` means the pid is not our child (a stale
    pid from a *previous* gateway, reparented to init, which reaps it) — not
    something to reap here, so liveness falls back to ``os.kill(pid, 0)``."""
    try:
        wpid, _ = os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        return False
    return wpid == pid


def kill_pid_group(pid: int, *, grace_s: float = 5.0) -> None:
    """SIGTERM the process group of ``pid``, wait ``grace_s``, SIGKILL if still
    alive, then reap the corpse so it does not linger as a ``<defunct>`` zombie.
    Safe to call on a stale PID — ProcessLookupError / ChildProcessError are
    swallowed (a previous gateway's child is reaped by init, not us).

    Note the reap is also what makes the grace loop terminate promptly for our
    own children: a zombie still answers ``os.kill(pid, 0)`` (the pid exists
    until reaped), so without ``waitpid`` the loop would spin the full grace
    period on every stop."""
    if pid <= 0:
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        _try_reap(pid)
        return
    except OSError as exc:
        log.warning("SIGTERM failed for pid=%d: %s", pid, exc)
        return
    deadline = _time.monotonic() + grace_s
    while _time.monotonic() < deadline:
        if _try_reap(pid):          # our child exited; zombie cleared
            return
        try:
            os.kill(pid, 0)         # still alive (ours, or a stale reparented one)
        except (ProcessLookupError, PermissionError):
            return
        _time.sleep(0.1)
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    # SIGKILL is asynchronous; give the corpse a brief window to appear, then
    # reap it. Bounded so a pid that isn't ours (never reapable here) can't hang.
    for _ in range(40):
        if _try_reap(pid):
            return
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        _time.sleep(0.05)


def default_hub_url() -> str:
    """Loopback URL of *this* gateway — the only correct ``AWM_HUB_URL`` to
    inject into a spawned service. ``config.PORT`` is the sandbox port in a dev
    sandbox and the prod port otherwise, so this is always right for the tree
    the gateway is running from."""
    return f"http://{config.HOST}:{config.PORT}/"


def spawn_and_journal(name: str, start_cmd: list[str], cwd: str,
                      hub_url: str | None = None) -> int:
    """Spawn a service from ``start_cmd`` and write its journal entry.

    The single spawn path shared by first-boot bootstrap and the
    ``POST /hub/services/{name}/start`` endpoint — both leave a journal entry a
    later reconcile can resume, and both inject exactly the three contract env
    vars (``AWM_HUB_URL`` / ``AWM_SERVICE_NAME`` / ``AWM_SERVICE_ID``). No auth.
    The fresh service self-registers and is assigned a ``service_id`` (filled
    into the journal by the register endpoint), so ``AWM_SERVICE_ID`` is empty.
    """
    hub_url = hub_url or default_hub_url()
    new_pid = spawn_service(name, list(start_cmd), cwd, {
        "AWM_HUB_URL": hub_url,
        "AWM_SERVICE_NAME": name,
        "AWM_SERVICE_ID": "",
    })
    update_service_journal_entry(name, {
        "start_cmd": list(start_cmd),
        "cwd": cwd,
        "last_pid": new_pid,
        "hub_url": hub_url,
        "prefix": f"/svc/{name}",
    })
    return new_pid


def _resolve_identity(name: str, entry: dict) -> tuple[list[str], str]:
    """The ``(start_cmd, cwd)`` to (re)launch a journaled service with.

    **Filesystem discovery wins.** If ``name`` is a real service folder under
    *this* gateway's ``services_root()``, its discovered ``start_cmd`` / ``cwd``
    are authoritative — a stale or clobbered journal (wrong worktree from a
    cross-tree contamination, or an empty ``start_cmd`` from a bad self-register)
    can never send the service to the wrong tree or silently strand it. Only a
    *non-discoverable* registration — an external service that registered over
    the wire with no folder here — falls back to the journal entry.

    This mirrors the existing precedent that already re-derives ``hub_url`` from
    live config instead of trusting the journal field (see
    ``_respawn_from_journal``).
    """
    from awm.gateway.hub import discovery as _discovery
    spec = _discovery.discover_service(name)
    if spec is not None:
        return list(spec.start_cmd), spec.cwd
    return list(entry.get("start_cmd") or []), entry.get("cwd") or ""


async def _reregister_record(name: str, entry: dict):
    """Re-create the registry record for a journaled service so its control-WS
    reconnect (carrying the journaled ``service_id``) is accepted.

    Shared by boot reconcile and the runtime disconnect watchdog. Returns the
    rehydrated ``ServiceRecord`` (or ``None`` if registration failed). Calls
    ``registry.register_service`` directly, bypassing the endpoint's
    duplicate-instance guard (T3) — that guard is for *new* instances, not for
    rehydrating a record we already own.

    Identity (``start_cmd`` / ``cwd``) comes from ``_resolve_identity`` so the
    record carries the *discovered* values for a discoverable service, not
    whatever a contaminated journal recorded.
    """
    from awm.gateway.hub.registry import get_registry
    registry = get_registry()
    sid = entry.get("service_id")
    prefix = entry.get("prefix") or f"/svc/{name}"
    start_cmd, cwd = _resolve_identity(name, entry)
    try:
        rec = await registry.register_service(
            name, prefix,
            pid=entry.get("last_pid"),
            start_cmd=list(start_cmd),
            cwd=cwd,
        )
        # Restore the journaled service_id so the service hits the same control
        # URL it had before the restart. registry.get_by_id reads service_id off
        # the record, so the rebind is sufficient.
        if sid:
            old = rec.service_id
            rec.service_id = sid
            log.debug("rebound service %s id %s->%s", name, old, sid)
        return rec
    except Exception as exc:
        log.warning("could not re-register journaled service %s: %s", name, exc)
        return None


async def _respawn_from_journal(name: str, entry: dict) -> None:
    """SIGTERM the stale PID (if any) and respawn the service from its journal
    entry. Shared by boot reconcile and the runtime disconnect watchdog.

    The caller is responsible for the higher-level gating (reconnected? enabled?
    still journaled?); this only does the kill-and-spawn, including the
    no-``start_cmd`` guard.

    Identity is resolved through ``_resolve_identity`` (discovery wins), so a
    discoverable service always respawns from *this* tree even if the journal
    names a wrong ``cwd`` or an empty ``start_cmd``; the resolved identity is
    written back into the journal, so a contaminated entry self-corrects on the
    first respawn (this is what retires the manual ``rm services.json``). Only a
    non-discoverable external registration can still hit the no-``start_cmd``
    guard.
    """
    sid = entry.get("service_id")
    last_pid = entry.get("last_pid")
    start_cmd, cwd = _resolve_identity(name, entry)
    if not start_cmd:
        log.warning(
            "service %s did not reconnect and has no start_cmd "
            "(not discoverable); leaving", name)
        return
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
            cwd,
            {
                "AWM_HUB_URL": hub_url,
                "AWM_SERVICE_NAME": name,
                "AWM_SERVICE_ID": sid or "",
            },
        )
        # Write back the resolved identity (not just the PID) so a stale/wrong
        # journal entry self-heals to the tree the gateway actually launched.
        update_service_journal_entry(name, {
            "last_pid": new_pid,
            "start_cmd": list(start_cmd),
            "cwd": cwd,
        })
    except (OSError, ValueError) as exc:
        log.error("respawn failed for service %s: %s", name, exc)


def _has_ready_control(sid: str | None) -> bool:
    """True iff the service holds an open, ready control channel."""
    if not sid:
        return False
    from awm.gateway.hub import rpc as _rpc
    ch = _rpc.get_control(sid)
    return ch is not None and ch.ready.is_set()


def pid_alive(pid: int | None) -> bool:
    """True iff ``pid`` names a live process. ``PermissionError`` (a pid we
    can't signal) still counts as alive; ``ProcessLookupError`` / other OS
    errors mean dead."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


async def _self_heal_once() -> None:
    """One self-heal sweep: re-bootstrap any journaled, enabled, non-overlay
    service whose last-known PID is dead and which holds no ready control.

    Closes the gap the disconnect watchdog leaves open. ``supervise_disconnect``
    only fires from the control-WS *close* path, so a service that died *before*
    ever opening its control WS — force-killed pre-handshake, or crashed during
    register — is never retried and sits ``starting`` with a dead PID forever
    (the wedge that needed a manual ``services restart``).

    Liveness is checked against the journal's ``last_pid`` — which
    ``_respawn_from_journal`` rewrites to the fresh PID on respawn — so a
    just-respawned or still-handshaking (PID-alive) service is skipped and the
    sweep cannot storm-respawn. A deliberate ``awm services stop`` drops the
    journal entry first, so stopped services are never seen here.
    """
    if is_shutting_down():
        return
    from awm.gateway.hub import discovery as _discovery
    for name, entry in list(load_service_journal().items()):
        if not isinstance(entry, dict):
            continue
        if not _discovery.is_enabled(name):
            continue
        if pid_alive(entry.get("last_pid")):
            continue
        if _has_ready_control(entry.get("service_id")):
            continue
        log.warning(
            "self-heal: service %s wedged (dead pid=%s, no ready control); "
            "re-bootstrapping", name, entry.get("last_pid"))
        await _respawn_from_journal(name, entry)


async def self_heal_loop() -> None:
    """Periodic wedged-service watchdog, started once at gateway boot.

    Runs forever on ``_SELF_HEAL_INTERVAL_S``; each tick is a best-effort sweep
    that never lets an exception kill the loop."""
    while not is_shutting_down():
        await asyncio.sleep(_SELF_HEAL_INTERVAL_S)
        try:
            await _self_heal_once()
        except Exception:
            log.debug("self-heal sweep failed", exc_info=True)


async def reconcile_journaled_services() -> None:
    """Boot-time reconcile loop.

    For each journaled service, give it a 10s window to reopen its
    control WS. If it doesn't, SIGTERM the last-known PID and respawn
    from start_cmd. Called from the FastAPI startup event after the
    hub control-plane routes are mounted.
    """
    journal = load_service_journal()
    if not journal:
        return
    deadline = asyncio.get_event_loop().time() + _RECONNECT_WINDOW_S
    for name, entry in list(journal.items()):
        if not isinstance(entry, dict):
            continue
        await _reregister_record(name, entry)

    # Park; let reconnects flow. After the window, respawn the silent ones.
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.5)

    from awm.gateway.hub import discovery as _discovery
    for name in list(journal.keys()):
        entry = journal.get(name) or {}
        if _has_ready_control(entry.get("service_id")):
            log.info("service %s reconnected within window", name)
            continue
        if not _discovery.is_enabled(name):
            # Operator disabled it while it was journaled; leave it down.
            log.info("service %s disabled; not respawning", name)
            continue
        await _respawn_from_journal(name, entry)


async def supervise_disconnect(name: str) -> None:
    """Runtime crash-respawn watchdog for a single service.

    Hooked from the control-WS disconnect path: a service whose control WS
    dropped unexpectedly (it crashed or was killed) gets ``_RECONNECT_WINDOW_S``
    to reconnect on its own; if it doesn't, it is respawned from the journal.

    Gated at every step so it never fights a deliberate stop or a gateway
    teardown:

    * ``is_shutting_down()`` — the gateway is tearing down; leave it alone.
    * journal entry present — ``awm services stop`` removes the entry *before*
      killing the process, so a deliberate stop is skipped.
    * ``discovery.is_enabled`` — a disabled service stays down.
    * not already reconnected — a genuine quick reconnect is a no-op.
    """
    from awm.gateway.hub import discovery as _discovery
    if is_shutting_down():
        return
    entry = load_service_journal().get(name)
    if not entry or not _discovery.is_enabled(name):
        return

    # Re-register so the service's own quick reconnect (same service_id) is
    # accepted rather than bounced with 4404 — eviction removed the record.
    await _reregister_record(name, entry)

    # Give it the window to come back.
    await asyncio.sleep(_RECONNECT_WINDOW_S)

    if is_shutting_down():
        return
    # Re-read: the entry may have been removed by a deliberate stop, or the
    # service may have reconnected, during the window.
    entry = load_service_journal().get(name)
    if not entry or not _discovery.is_enabled(name):
        return
    if _has_ready_control(entry.get("service_id")):
        log.info("service %s reconnected after disconnect; not respawning", name)
        return
    log.info("service %s did not reconnect within %.0fs; respawning",
             name, _RECONNECT_WINDOW_S)
    await _respawn_from_journal(name, entry)


async def bootstrap_discovered_services() -> None:
    """First-boot bootstrap: spawn every discovered, enabled service that the
    journal has never seen.

    Runs *after* ``reconcile_journaled_services`` — which already owns every
    name in the journal (reconnect or respawn). So this only fires for services
    the journal has no record of: a fresh clone, a wiped ``services.json``, or a
    newly-added service folder. Once a service is journaled here, the next
    gateway restart routes it through reconcile, not bootstrap (so a second
    restart never double-spawns).

    The spawn path, ``start_cmd`` (``["bash", "run.sh"]``) and ``cwd`` are
    identical to what a service self-registers with, so a bootstrap-spawned
    entry is indistinguishable from a self-registered one to reconcile.
    """
    from awm.gateway.hub import discovery
    from awm.gateway.hub.registry import get_registry

    journal = load_service_journal()
    registry = get_registry()
    for spec in discovery.discover_services():
        if not spec.enabled:
            log.info("bootstrap: %s disabled; skipping", spec.name)
            continue
        if spec.name in journal:
            continue  # reconcile owns it
        if registry.get_by_name("service", spec.name) is not None:
            continue  # already self-registered independently
        try:
            new_pid = spawn_and_journal(spec.name, list(spec.start_cmd), spec.cwd)
        except (OSError, ValueError) as exc:
            log.error("bootstrap spawn failed for %s: %s", spec.name, exc)
            continue
        log.info("bootstrap: spawned %s pid=%d", spec.name, new_pid)


async def bootstrap_discovered_pages() -> None:
    """Boot-time page bootstrap: register every discovered page bundle
    (``awm/pages/<name>`` with a built ``dist/``) as a ``/ui/<name>`` base.

    Pages are static and hold no control WS, so there is nothing to journal or
    reconcile — a page base is pure in-RAM routing state that a restart drops.
    Unlike a service, a page can never re-register itself, so re-deriving it
    from the filesystem on every boot is the *only* thing that keeps ``/ui/...``
    pages alive across a ``systemctl restart``.

    Idempotent: ``register_page`` replaces a same-name base in place, so a
    re-run is harmless. A prefix already owned by a *different* name is logged
    and skipped — one bad page never aborts the loop.
    """
    from awm.gateway.hub import discovery
    from awm.gateway.hub.registry import PrefixConflict, get_registry

    registry = get_registry()
    for spec in discovery.discover_pages():
        try:
            await registry.register_page(spec.name, spec.prefix, spec.dist_dir)
        except PrefixConflict as exc:
            log.warning("bootstrap page %s: %s; skipping", spec.name, exc)
        except Exception as exc:  # noqa: BLE001
            log.error("bootstrap page %s failed: %s", spec.name, exc)
        else:
            log.info("bootstrap: page %s → %s", spec.name, spec.prefix)

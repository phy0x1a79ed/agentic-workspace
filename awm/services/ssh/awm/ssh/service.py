from __future__ import annotations

import asyncio
import enum
import fcntl
import json
import logging
import os
import secrets
import stat
import time
from dataclasses import dataclass
from typing import Any

from awm import gatewayclient
from awm.ssh.config import (
    KNOWN_HOSTS,
    LIVE_DIR,
    LOCK_DIR,
    SINGLETON_PATH,
    SSH_ASKPASS,
    HostConfig,
    lock_path,
    resolve_host,
    stderr_path,
)

log = logging.getLogger("awm.ssh.service")

_CONNECT_TIMEOUT = 120.0
_CHECK_POLL_INTERVAL = 1.5
_CHECK_POLL_ATTEMPTS = 40
_DISCONNECT_POLL_ATTEMPTS = 8
# Hard cap on a teardown (`ssh -O exit` + confirm). Bounds the otherwise-unbounded
# `proc.communicate()` so a wedged control socket can't hang a teardown forever —
# which, on the connect timeout path, would strand the host in AUTHENTICATING with
# the breaker never tripping and every later connect absorbed onto a dead waiter.
_EXIT_TIMEOUT = 15.0
# Hard cap on a single `ssh -O check` probe. These are local control-socket probes
# (no remote round-trip) that normally return in well under a second, but a WEDGED
# master can make `-O check` block indefinitely. Because connect/disconnect now
# await boot reconciliation, which runs these probes, an unbounded probe would hang
# every verb forever — so each probe is bounded and a timeout is read as "not live".
_CHECK_TIMEOUT = 10.0
# Backstop: however slow boot reconciliation gets, a verb waits at most this long
# for it before proceeding (reconcile then continues in the background). With the
# per-probe cap above reconcile is already bounded; this only guards a pathological
# pile-up of simultaneously-wedged probes.
_RECONCILE_WAIT_TIMEOUT = 60.0

# Discord notifications target for lockout alerts: unimatrix0#notifications.
_ALERT_ACCOUNT = "discord-bot"
_ALERT_CHANNEL = "1522674357762261112"

# Singleton re-homing selectors (federation). When 2fa / social run as canonical
# singletons on a peer node, this node borrows them by exporting these to the
# peer's name (e.g. AWM_TWOFA_PEER=mira, AWM_SOCIAL_PEER=mira). Unset/empty ⇒ the
# service is local and every call stays on this gateway. Read fresh per use via
# gatewayclient.peer_env so a reconnect picks up a change without a restart.
_TWOFA_PEER_ENV = "AWM_TWOFA_PEER"
_SOCIAL_PEER_ENV = "AWM_SOCIAL_PEER"
# Connection-slot arbiter selector (federation). For a lockout-sensitive host,
# EXACTLY ONE attempt may be in flight across the whole fleet — the per-node
# breaker isn't enough once several nodes drive the same account. So a gated
# connect first acquires a slot from the single arbiter (ssh@<peer>). Set to the
# arbiter node's name (e.g. AWM_SSH_SLOT_PEER=mira) on every borrowing node; the
# node that OWNS the arbiter leaves it unset and acquires its slot in-process.
_SLOT_PEER_ENV = "AWM_SSH_SLOT_PEER"

# How long after a Discord /approve the operator-approval window stays open for
# a device. A locked host may re-connect only while this window is open. Kept
# ≥ the 2fa social burst window so the auto-approver is still armed for the
# connect the approval authorizes.
_APPROVE_WINDOW_SECONDS = 300.0

# Notable substrings in ssh stderr worth surfacing into ssh.log / the lock reason.
_NOTABLE_STDERR = (
    "Permission denied",
    "Too many authentication failures",
    "Authentication failed",
    "locked",
    "MFA",
    "Duo",
    "Connection refused",
    "Connection timed out",
    "Could not resolve hostname",
    "Operation timed out",
)


class ConnState(enum.Enum):
    """The single canonical per-host connection lifecycle.

    Every host moves DISCONNECTED → AUTHENTICATING → CONNECTED → DISPOSING →
    DISCONNECTED and nowhere else. ``connect`` is the sole entry; duplicate/
    concurrent requests are absorbed into the one in-flight attempt rather than
    minting a second ssh/MFA. A breaker "locked" host is DISCONNECTED with a
    lockfile on disk — not a distinct state.
    """

    DISCONNECTED = "disconnected"
    AUTHENTICATING = "authenticating"
    CONNECTED = "connected"
    DISPOSING = "disposing"


@dataclass
class HostState:
    """Per-host state object. Mutated only under ``SSHService._lock`` — a single
    synchronous critical section per transition (no ``await`` between reading the
    state and writing the next one), which is what makes the dedup race-free on
    the cooperative event loop."""

    state: ConnState = ConnState.DISCONNECTED
    attempt: asyncio.Task | None = None   # the one in-flight auth (AUTHENTICATING)
    disposal: asyncio.Task | None = None  # the one in-flight teardown (DISPOSING)
    # A disconnect issued during AUTHENTICATING is not allowed to abort the auth;
    # it sets this flag and is honoured once the attempt resolves.
    pending_disconnect: bool = False


class _AttemptFailed(Exception):
    """Raised inside an attempt when the ControlMaster never came up. Carries the
    base failure reason; the caller enriches it with stderr / askpass deviation."""


class SSHService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._hosts: dict[str, HostState] = {}
        # device -> monotonic deadline until which an operator approval (a
        # Discord /approve on that device) is considered active. Recovery from a
        # tripped breaker happens ONLY inside such a window; there is no verb.
        self._approve_until: dict[str, float] = {}
        # Slot arbiter (federation): host -> lease_id of the ONE in-flight attempt
        # holding this host's slot fleet-wide. Guards the LEASED state of the
        # SlotArbiter DFA (IDLE/LEASED/LOCKED); LOCKED is the on-disk lockfile
        # (authoritative, central on the arbiter node), IDLE is the absence of
        # both. Populated only on the node that owns the arbiter (or in-process
        # for that node's own gated attempts). See _slot_acquire / _slot_release.
        self._leased: dict[str, str] = {}
        self._arbiter_lock = asyncio.Lock()
        self._social_task: asyncio.Task | None = None
        self._reconcile_task: asyncio.Task | None = None
        # Held open for the process lifetime once acquired, so the flock stays
        # held (releasing on process death). Stored to keep the fd from being GC'd.
        self._singleton_fd: int | None = None

    def init(self) -> None:
        # Enforce the process singleton BEFORE anything else — if another svc-ssh
        # already holds the lock, stand down cleanly rather than racing the same
        # account. os._exit (not sys.exit) so it's a clean process exit and never
        # an exception into the adapter.
        self._acquire_singleton()
        os.makedirs(LIVE_DIR, exist_ok=True)
        os.makedirs(LOCK_DIR, exist_ok=True)
        # Subscribe to the social service's slash commands so a Discord /approve
        # opens the recovery window (best-effort + self-reconnecting; inert when
        # no social service is present). Mirrors the 2fa service's listener.
        try:
            loop = asyncio.get_running_loop()
            self._social_task = loop.create_task(self._approve_listener())
            # Rebuild per-host state from the world (adopt live masters, respect
            # breaker locks) and reap stale sockets — in the background so startup
            # never blocks on ssh probes.
            self._reconcile_task = loop.create_task(self._reconcile_on_boot())
        except RuntimeError as exc:  # no running loop (shouldn't happen at on_start)
            log.warning("ssh: startup tasks not started: %s", exc)
        log.info("ssh service initialised (live_connections: %s)", LIVE_DIR)

    def _acquire_singleton(self) -> None:
        try:
            fd = os.open(SINGLETON_PATH, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            # Can't even open the lock file — fail open rather than crash; the
            # gateway already scopes one service per name in normal operation.
            log.error("ssh: cannot open singleton lock %s: %s", SINGLETON_PATH, exc)
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log.error("ssh: another svc-ssh already holds %s — standing down so "
                      "two instances can't race the same account", SINGLETON_PATH)
            os.close(fd)
            os._exit(0)
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        except OSError:
            pass
        self._singleton_fd = fd

    async def _reconcile_on_boot(self) -> None:
        """Reconstruct per-host state from reality after a (re)start: adopt any
        live ControlMaster, leave breaker-locked hosts alone, then reap stale
        control sockets left by a killed process."""
        try:
            for name, cfg in KNOWN_HOSTS.items():
                if await self._check_master(cfg.host):
                    hs = self._host(name)
                    async with self._lock:
                        if hs.state == ConnState.DISCONNECTED:
                            hs.state = ConnState.CONNECTED
                    # A live master is proof of a real successful auth, so it
                    # supersedes any leftover breaker lock (mirrors the runtime
                    # probe_start path). Without this, a host adopted CONNECTED but
                    # still carrying a stale lockfile would, once that master later
                    # dies and it demotes, read the stale lock and refuse to
                    # reconnect — an unrecoverable-looking hold with no real fault.
                    self._clear_lock(cfg)
                    log.info("ssh: reconciled %s → connected (adopted live master)",
                             name)
            await self._reap_orphans()
        except Exception as exc:  # noqa: BLE001 — reconciliation is best-effort
            log.warning("ssh: boot reconciliation failed: %s", exc)

    async def _reap_orphans(self) -> None:
        """Remove dead ControlMaster socket files in LIVE_DIR — leftovers from a
        killed process. Conservative: a socket with a *live* master answers the
        probe and is left untouched (adoption above already claimed known ones)."""
        try:
            entries = os.listdir(LIVE_DIR)
        except OSError:
            return
        for fn in entries:
            path = os.path.join(LIVE_DIR, fn)
            try:
                if not stat.S_ISSOCK(os.stat(path).st_mode):
                    continue  # skip stderr / marker / lock regular files
            except OSError:
                continue
            if not await self._check_socket(path):
                # Double-check before reaping: a transient false-negative (a live
                # master briefly not answering `-O check` under load) must not get
                # its socket unlinked, which would force the next connect to re-auth
                # — a fresh, avoidable MFA. Only reap a socket that fails twice.
                await asyncio.sleep(0.5)
                if await self._check_socket(path):
                    continue
                log.info("ssh: reaping stale control socket %s", fn)
                self._safe_unlink(path)

    @staticmethod
    async def _run_check(argv: list[str]) -> bool:
        """Run an ``ssh -O check`` variant, bounded by ``_CHECK_TIMEOUT``. A wedged
        control master can make ``-O check`` hang forever; since reconcile (and thus
        the verbs that now await it) depend on these probes, we cap each one and
        read a timeout as 'not live' (False), killing the stuck probe so it can't
        leak. Never raises."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning("ssh: could not spawn probe %s: %s", argv, exc)
            return False
        try:
            await asyncio.wait_for(proc.communicate(), timeout=_CHECK_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("ssh: probe %s wedged >%.0fs — treating as not live",
                        argv, _CHECK_TIMEOUT)
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, Exception):  # noqa: BLE001
                pass
            return False
        return proc.returncode == 0

    @staticmethod
    async def _check_socket(path: str) -> bool:
        """True iff a live master answers on the given control socket path."""
        return await SSHService._run_check(
            ["ssh", "-O", "check", "-S", path, "reap-probe"])

    # -- state access -------------------------------------------------------

    def _host(self, name: str) -> HostState:
        hs = self._hosts.get(name)
        if hs is None:
            hs = HostState()
            self._hosts[name] = hs
        return hs

    # -- public verbs -------------------------------------------------------

    async def _await_reconcile(self) -> None:
        """Block until boot reconciliation (master adoption + orphan reap) has
        finished. A verb must not start an ssh attempt while ``_reap_orphans`` may
        still be unlinking sockets — otherwise an in-flight attempt's half-open
        socket could be reaped out from under it. Awaiting a Task here does not
        cancel it if this verb is itself cancelled, so a slow verb can't abort the
        shared reconcile."""
        task = self._reconcile_task
        if task is None or task.done():
            return
        try:
            # shield: a timeout (or a cancelled verb) must not cancel the shared
            # reconcile task — it keeps running for the next verb.
            await asyncio.wait_for(asyncio.shield(task),
                                   timeout=_RECONCILE_WAIT_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("ssh: boot reconcile still running after %.0fs — proceeding "
                        "(it continues in the background)", _RECONCILE_WAIT_TIMEOUT)
        except Exception:  # noqa: BLE001 — reconcile is best-effort; proceed anyway
            pass

    async def connect(self, host: str) -> dict:
        cfg = resolve_host(host)
        hs = self._host(host)
        await self._await_reconcile()

        while True:
            async with self._lock:
                st = hs.state
                if st == ConnState.AUTHENTICATING:
                    waiter, kind = hs.attempt, "attempt"     # absorb
                elif st == ConnState.DISPOSING:
                    waiter, kind = hs.disposal, "dispose"    # defer
                elif st == ConnState.CONNECTED:
                    kind = "probe_connected"                 # verify liveness
                else:  # DISCONNECTED
                    # Circuit breaker FIRST — a held host does zero ssh/VPN/2FA
                    # work (not even an auth-free probe), so it can never march
                    # toward the provider's lockout ceiling. The hold lifts only
                    # while an operator approval window is open.
                    if self._read_lock(cfg) is not None:
                        if not self._approve_active(cfg.twofa_device):
                            return self._status_dict(
                                cfg, "unavailable",
                                error=f"{cfg.host} is not available for automated "
                                      f"access right now")
                        log.info("operator approval window open for %s (device %s) "
                                 "— clearing hold and reconnecting (one-shot)",
                                 cfg.host, cfg.twofa_device)
                        self._clear_lock(cfg)
                        # ONE-SHOT: consume the window as we spend it. Each operator
                        # /approve authorises exactly one reconnect attempt. Without
                        # this the window stays open for its full duration, so a
                        # caller that retries a failing connect re-trips then
                        # re-clears the breaker every iteration — an unbounded run of
                        # Duo pushes straight to the provider lockout the breaker
                        # exists to prevent. Consuming here also bounds the
                        # device-keyed window: only the first host on a shared device
                        # recovers per /approve, not every host sharing it.
                        self._approve_until.pop(cfg.twofa_device, None)
                    kind = "probe_start"

            if kind == "attempt":
                return await self._await_result(waiter, cfg)
            if kind == "dispose":
                # Let the teardown finish, then re-evaluate (→ DISCONNECTED →
                # a fresh attempt).
                await self._await_quietly(waiter)
                continue
            if kind == "probe_connected":
                # AUTH-FREE probe (`ssh -O check` never mints a login). Live →
                # done; dead → the master died out-of-band, demote and re-auth.
                if await self._check_master(cfg.host):
                    return self._status_dict(cfg, "connected")
                async with self._lock:
                    if hs.state == ConnState.CONNECTED:
                        hs.state = ConnState.DISCONNECTED
                continue

            # kind == "probe_start": past the breaker gate. Probe (auth-free) for
            # an already-live master — ours, adopted at boot, or made out-of-band
            # — so we never spawn `ssh -M` against an existing socket (which would
            # drop to a non-multiplexed login and fire MFA).
            if await self._check_master(cfg.host):
                async with self._lock:
                    if hs.state in (ConnState.DISCONNECTED, ConnState.CONNECTED):
                        hs.state = ConnState.CONNECTED
                        self._clear_lock(cfg)  # a live master supersedes a stale lock
                        return self._status_dict(cfg, "connected")
                continue
            # No master, not locked: CAS to AUTHENTICATING and start the one
            # attempt. Re-validate under the lock so a connect that raced us into
            # AUTHENTICATING is absorbed rather than duplicated.
            async with self._lock:
                if hs.state != ConnState.DISCONNECTED:
                    continue
                hs.state = ConnState.AUTHENTICATING
                hs.pending_disconnect = False
                hs.attempt = asyncio.create_task(self._run_attempt(cfg, hs))
                waiter = hs.attempt
            return await self._await_result(waiter, cfg)

    async def disconnect(self, host: str) -> dict:
        cfg = resolve_host(host)
        hs = self._host(host)
        await self._await_reconcile()

        while True:
            async with self._lock:
                st = hs.state
                if st == ConnState.DISCONNECTED:
                    return self._status_dict(cfg, "disconnected")
                if st == ConnState.AUTHENTICATING:
                    # Hold — do NOT abort the in-flight auth. Mark intent and let
                    # it resolve, keeping the single canonical path.
                    hs.pending_disconnect = True
                    waiter, kind = hs.attempt, "attempt"
                elif st == ConnState.CONNECTED:
                    hs.state = ConnState.DISPOSING
                    hs.disposal = asyncio.create_task(self._run_dispose(cfg, hs))
                    waiter, kind = hs.disposal, "dispose"
                else:  # DISPOSING
                    waiter, kind = hs.disposal, "dispose"    # absorb

            if kind == "attempt":
                # Wait for auth to resolve, then loop: a successful connect will
                # have queued disposal (→ DISPOSING), a failed one is already
                # DISCONNECTED. Either way the next pass finishes the disconnect.
                await self._await_quietly(waiter)
                continue
            cleared = await self._await_bool(waiter)
            if cleared:
                return self._status_dict(cfg, "disconnected")
            return self._status_dict(cfg, "disconnected",
                                     warning="master process may still be running")

    async def status(self) -> dict:
        connections: dict[str, dict] = {}
        for name, cfg in KNOWN_HOSTS.items():
            hs = self._host(name)
            st = hs.state
            if st == ConnState.CONNECTED:
                connections[name] = self._status_dict(cfg, "connected")
            elif st == ConnState.AUTHENTICATING:
                connections[name] = self._status_dict(cfg, "connecting")
            elif st == ConnState.DISPOSING:
                connections[name] = self._status_dict(cfg, "disconnecting")
            elif self._read_lock(cfg) is not None:
                # Held by the breaker. Reported neutrally — no mechanism detail.
                connections[name] = self._status_dict(cfg, "unavailable")
            else:
                connections[name] = self._status_dict(cfg, "disconnected")
        return {"connections": connections}

    # -- attempt / disposal drivers -----------------------------------------

    async def _run_attempt(self, cfg: HostConfig, hs: HostState) -> dict:
        """The single auth attempt. Always resolves via an internal outcome — a
        success or a breaker trip — never by outer cancellation, so the breaker
        can never be bypassed. Transitions the host on completion."""
        try:
            result = await self._do_connect(cfg)
        except Exception as e:  # noqa: BLE001
            # _do_connect is contracted never to raise, but guard anyway: an
            # unexpected raise (e.g. OSError writing the lockfile under disk/fd
            # exhaustion, which is outside _do_connect's own try) must NEVER leave
            # the host stranded in AUTHENTICATING — every later connect would absorb
            # onto this dead attempt and return error forever, with the breaker
            # never tripped. Force a terminal state and best-effort hold the host.
            log.error("connect to %s raised unexpectedly: %s", cfg.host, e)
            try:
                await self._trip_breaker(cfg, f"unexpected error: {e}")
            except Exception as e2:  # noqa: BLE001 — breaker is best-effort here
                log.error("could not trip breaker for %s after error: %s",
                          cfg.host, e2)
            result = self._status_dict(cfg, "error",
                                       error=f"connect to {cfg.host} failed")
        connected = result.get("status") == "connected"
        async with self._lock:
            hs.attempt = None
            want_disconnect = hs.pending_disconnect
            hs.pending_disconnect = False
            if connected and want_disconnect:
                # A disconnect was queued during auth: finish the canonical path
                # (we DID connect) and immediately dispose.
                hs.state = ConnState.DISPOSING
                hs.disposal = asyncio.create_task(self._run_dispose(cfg, hs))
            elif connected:
                hs.state = ConnState.CONNECTED
            else:
                # Failure already tripped the breaker inside _do_connect.
                hs.state = ConnState.DISCONNECTED
        return result

    async def _run_dispose(self, cfg: HostConfig, hs: HostState) -> bool:
        cleared = False
        try:
            cleared = await self._exit_master(cfg.host)
        finally:
            async with self._lock:
                hs.disposal = None
                hs.state = ConnState.DISCONNECTED
        return cleared

    async def _await_result(self, waiter: asyncio.Task, cfg: HostConfig) -> dict:
        try:
            return await waiter
        except Exception as e:  # noqa: BLE001 — an attempt should never raise out
            log.error("attempt for %s failed unexpectedly: %s", cfg.host, e)
            return self._status_dict(cfg, "error",
                                     error=f"connect to {cfg.host} failed")

    @staticmethod
    async def _await_quietly(waiter: asyncio.Task | None) -> None:
        if waiter is None:
            return
        try:
            await waiter
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    async def _await_bool(waiter: asyncio.Task | None) -> bool:
        if waiter is None:
            return True
        try:
            return bool(await waiter)
        except Exception:  # noqa: BLE001
            return False

    # -- the actual ssh work ------------------------------------------------

    async def _do_connect(self, cfg: HostConfig) -> dict:
        """Route the single attempt. A lockout-sensitive host (one with a 2FA
        device) goes through the fleet-global slot arbiter — a live-WS lease to
        ``ssh@<AWM_SSH_SLOT_PEER>``, or the in-process arbiter when this node owns
        the slot (selector unset). Every other host keeps the per-node
        local-breaker path unchanged. Never raises."""
        if cfg.twofa_device:
            return await self._connect_through_arbiter(cfg)
        return await self._do_connect_attempt(cfg, gated=False)

    # -- slot arbiter: fleet-global single-attempt gate ---------------------

    async def _connect_through_arbiter(self, cfg: HostConfig) -> dict:
        """Acquire the host's fleet-wide slot, run the one attempt while holding
        it, then report the verdict. Local when we own the arbiter, else a live-WS
        lease to the arbiter peer. Fails CLOSED — an unreachable arbiter refuses
        the connect (no VPN/2FA/ssh, no MFA spent)."""
        slot_peer = gatewayclient.peer_env(_SLOT_PEER_ENV)
        if not slot_peer:
            return await self._connect_via_local_arbiter(cfg)
        return await self._connect_via_arbiter_peer(cfg, slot_peer)

    async def _connect_via_local_arbiter(self, cfg: HostConfig) -> dict:
        """This node owns the arbiter: acquire in-process, hold across the attempt
        (the attempt task's liveness IS the hold), release with the verdict."""
        status, token = await self._slot_acquire(cfg.host)
        if status != "granted":
            return self._status_dict(
                cfg, "unavailable",
                error=f"{cfg.host} is not available for automated access right now")
        ok = False
        reason = ""
        try:
            result = await self._do_connect_attempt(cfg, gated=True)
            ok = result.get("status") == "connected"
            reason = result.pop("_lock_reason", "")
            return result
        finally:
            await self._slot_release(cfg.host, token, ok=ok, reason=reason)

    async def _connect_via_arbiter_peer(self, cfg: HostConfig,
                                        slot_peer: str) -> dict:
        """Acquire a slot from the arbiter peer over a CA-verified, peer-bearer
        WS lease; the open socket IS the lease. Hold it across the attempt, then
        send the verdict (a clean release) — a drop mid-attempt trips the arbiter
        LOCKED on its own."""
        try:
            lease = await gatewayclient.acquire_lease_maybe_peer(
                slot_peer, "ssh", cfg.host)
        except Exception as e:  # noqa: BLE001 — arbiter unreachable → FAIL CLOSED
            log.error("connection arbiter %s unreachable for %s: %s",
                      slot_peer, cfg.host, e)
            return self._status_dict(
                cfg, "unavailable",
                error=f"{cfg.host} is not available for automated access right now")
        async with lease:
            if not lease.granted:
                log.info("slot arbiter %s refused %s: %s (%s)",
                         slot_peer, cfg.host, lease.status, lease.reason)
                return self._status_dict(
                    cfg, "unavailable",
                    error=f"{cfg.host} is not available for automated access right now")
            result = await self._do_connect_attempt(cfg, gated=True)
            ok = result.get("status") == "connected"
            reason = result.pop("_lock_reason", "")
            await lease.verdict(ok=ok, reason=reason)
            return result

    async def _slot_acquire(self, host: str) -> tuple[str, str | None]:
        """Arbiter ``open`` transition. Atomically grant at most one in-flight
        lease per host: returns ``("granted", lease_id)`` from IDLE,
        ``("busy", None)`` from LEASED, ``("locked", reason)`` from LOCKED — unless
        an operator approval window is open, which clears the hold and grants
        (mirroring the one-shot window consumption in :meth:`connect`)."""
        cfg = resolve_host(host)
        async with self._arbiter_lock:
            if host in self._leased:
                return ("busy", None)
            if self._read_lock(cfg) is not None:
                if not self._approve_active(cfg.twofa_device):
                    return ("locked", "held after a prior failed connect")
                # Approval window open → clear + consume one-shot (see connect()).
                self._clear_lock(cfg)
                self._approve_until.pop(cfg.twofa_device, None)
            token = secrets.token_urlsafe(8)
            self._leased[host] = token
            return ("granted", token)

    async def _slot_release(self, host: str, lease_id: str | None, *,
                            ok: bool, reason: str = "") -> None:
        """Arbiter ``verdict_ok`` / ``verdict_fail`` / ``drop`` transition.
        ``ok`` frees the slot (→ IDLE, clearing any lock); otherwise → LOCKED
        (persist the lockfile) and page the operator — the arbiter is the SOLE
        notifier on a LOCKED transition, so the requester's attempt stays silent.
        Idempotent by ``lease_id``: a duplicate or stale release is a no-op."""
        cfg = resolve_host(host)
        do_alert = False
        async with self._arbiter_lock:
            if lease_id is not None and self._leased.get(host) != lease_id:
                return  # superseded / already released — ignore
            self._leased.pop(host, None)
            if ok:
                self._clear_lock(cfg)
                return
            self._write_lock(cfg, reason or "attempt failed (slot arbiter)")
            do_alert = True
        if do_alert:
            log.error("BREAKER TRIPPED (arbiter) — holding %s: %s", host, reason)
            await self._alert(self._lock_alert_text(cfg, reason))

    async def _lease_session(self, ctx: Any) -> None:
        """Direct-session handler (the arbiter side of a WS lease). The OPEN bridge
        socket IS the lease: grant or refuse on the first frame, then hold until
        the requester reports a ``verdict`` or the socket drops. Drop without a
        verdict is treated as a failed attempt (LOCKED) — the ZooKeeper-ephemeral
        semantics that make a dead holder free (or trip) its slot at once."""
        host = (ctx.init or {}).get("host", "")
        bridge = await ctx.open_bridge()
        token: str | None = None
        try:
            try:
                resolve_host(host)
            except ValueError:
                await bridge.send(json.dumps(
                    {"lease": "error", "reason": f"unknown host {host!r}"}))
                return
            status, detail = await self._slot_acquire(host)
            frame = ({"lease": "granted"} if status == "granted"
                     else {"lease": status, "reason": detail or status})
            await bridge.send(json.dumps(frame))
            if status != "granted":
                return
            token = detail
            verdict: str | None = None
            vreason = ""
            try:
                async for raw in bridge:
                    if isinstance(raw, (bytes, bytearray)):
                        continue
                    try:
                        msg = json.loads(raw) or {}
                    except json.JSONDecodeError:
                        continue
                    v = msg.get("verdict")
                    if v in ("ok", "fail"):
                        verdict = v
                        vreason = msg.get("reason") or ""
                        break
            except Exception:  # noqa: BLE001 — socket dropped mid-hold
                pass
            if verdict == "ok":
                await self._slot_release(host, token, ok=True)
            else:
                reason = (vreason or "requester reported connect failure"
                          if verdict == "fail"
                          else "requester dropped the lease without a verdict")
                await self._slot_release(host, token, ok=False, reason=reason)
            token = None
        finally:
            if token is not None:
                # Aborted after grant without a clean release — trip closed (safe).
                await self._slot_release(host, token, ok=False,
                                         reason="lease handler aborted")
            try:
                await bridge.close()
            except Exception:  # noqa: BLE001
                pass

    async def _do_connect_attempt(self, cfg: HostConfig, *,
                                  gated: bool = False) -> dict:
        """Bring up the ControlMaster (vpn + 2fa + ssh + poll), bounded by an
        INTERNAL timeout. Any failure — including the timeout — trips the breaker
        and returns an error dict; success returns a connected dict. Never raises.

        When ``gated`` the slot arbiter owns the breaker: a failure returns an
        error dict WITHOUT tripping the local breaker or alerting, stashing the
        reason under ``_lock_reason`` so the caller can pass it up as the lease
        verdict — the arbiter records LOCKED and pages the operator exactly once."""
        marker = self._deviation_marker(cfg)
        self._safe_unlink(marker)
        try:
            await asyncio.wait_for(self._attempt_master(cfg, marker),
                                   timeout=_CONNECT_TIMEOUT)
            log.info("connected to %s", cfg.host)
            return self._status_dict(cfg, "connected")
        except _AttemptFailed as e:
            # The poll window (_CHECK_POLL_ATTEMPTS * _CHECK_POLL_INTERVAL) is
            # shorter than the wait_for cap, so a master whose auth completed late
            # can appear just after we gave up polling — and this, not TimeoutError,
            # is the common give-up path. Re-check once: if it's now up, ADOPT it as
            # success. That auth already succeeded (an MFA attempt was spent and
            # approved), so tripping the breaker on it would waste a live connection
            # AND spuriously page the operator, while leaving a master orphaned
            # behind a lock. Only if there is genuinely no master do we fail through.
            if await self._check_master(cfg.host):
                self._clear_lock(cfg)
                log.info("connected to %s (master appeared just after poll window)",
                         cfg.host)
                return self._status_dict(cfg, "connected")
            reason = self._failure_reason(cfg, marker, str(e))
        except asyncio.TimeoutError:
            # Reap a possibly half-open master so the hung attempt can't linger.
            await self._exit_master(cfg.host)
            reason = self._failure_reason(
                cfg, marker, f"connect exceeded {_CONNECT_TIMEOUT:.0f}s")
        except Exception as e:  # noqa: BLE001
            log.error("connect to %s failed: %s", cfg.host, e)
            reason = self._failure_reason(cfg, marker, str(e))
        # Neutral to the caller — the detailed reason goes to the lock + the
        # operator Discord alert, not to the agent.
        result = self._status_dict(cfg, "error", error=f"connect to {cfg.host} failed")
        if not gated:
            await self._trip_breaker(cfg, reason)
        else:
            # The slot arbiter owns LOCKED + the alert (via the lease verdict);
            # surface the reason so the caller can hand it up.
            result["_lock_reason"] = reason
        return result

    async def _attempt_master(self, cfg: HostConfig, marker: str) -> None:
        """Orchestrate vpn + 2fa + ssh and poll for the ControlMaster socket.
        Returns on success (lock cleared); raises :class:`_AttemptFailed` if the
        master never appears."""
        log.info("connecting to %s (vpn=%s, 2fa=%s)",
                 cfg.host, cfg.vpn_profile or "none",
                 cfg.twofa_device or "none")

        if cfg.needs_vpn and cfg.vpn_profile:
            vpn_result = await gatewayclient.call(
                "vpn", "up", {"profile": cfg.vpn_profile})
            log.info("vpn up %s: %s", cfg.vpn_profile,
                     vpn_result.get("status", "ok"))

        if cfg.twofa_device:
            # Always arm +1: this connect is about to fire exactly ONE Duo push, so
            # it must contribute exactly one approval to the device's budget. The
            # 2fa start_burst verb accumulates overlapping arms (grant totals N) and
            # reuses the single poll task, so arming unconditionally is correct and
            # never double-spawns. Do NOT gate on an existing "burst_active": a
            # second concurrent connect to a host sharing this device (e.g.
            # sockeye/sockeye1 both on cwl) would then skip its grant, leaving budget
            # at 1 for 2 pushes — the 2nd push is held, times out, and trips its
            # breaker. That was the original overlapping-login failure.
            await gatewayclient.call_maybe_peer(
                gatewayclient.peer_env(_TWOFA_PEER_ENV),
                "2fa", "burst", {
                    "device": cfg.twofa_device,
                    "window": 120,
                    "count": 1,
                })
            log.info("2fa burst armed (+1) for %s on device %s%s",
                     cfg.host, cfg.twofa_device,
                     f" via 2fa@{gatewayclient.peer_env(_TWOFA_PEER_ENV)}"
                     if gatewayclient.peer_env(_TWOFA_PEER_ENV) else "")

        env = os.environ.copy()
        env.update({
            "SSH_ASKPASS": SSH_ASKPASS,
            "SSH_ASKPASS_REQUIRE": "force",
            "AWM_DUO_DEVICES": "awm|Mira",
            # The askpass drops this marker (and refuses) on any prompt it
            # can't exactly character-match to one of our devices, so the
            # failure path below can report "askpass deviation" as the cause.
            "AWM_SSH_ASKPASS_MARKER": marker,
        })

        # A guarded host carries a ProxyCommand guard in ~/.ssh/config that
        # blocks bare `ssh <host>` when no master exists. The service is the
        # sole allowed master-creator, so it overrides the guard here.
        # (Command-line `-o` is first-match-wins over config.) Do NOT do this
        # for VPN-bounced hosts — their ProxyCommand is the required tunnel.
        argv = ["ssh", "-f", "-N", "-M"]
        if cfg.guarded:
            argv += ["-o", "ProxyCommand=none"]
        argv.append(cfg.host)

        # Capture ssh stderr to a file (NOT a PIPE — a PIPE makes the forked
        # `-f -N -M` child hang, per the README). This is what makes a failed
        # connect visible instead of silently swallowed.
        errfile = stderr_path(cfg)
        with open(errfile, "wb") as ef:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=ef,
            )
            await proc.wait()

        for _ in range(_CHECK_POLL_ATTEMPTS):
            if await self._check_master(cfg.host):
                self._clear_lock(cfg)
                return
            await asyncio.sleep(_CHECK_POLL_INTERVAL)

        raise _AttemptFailed(
            f"ControlMaster did not appear within "
            f"{_CHECK_POLL_ATTEMPTS * _CHECK_POLL_INTERVAL:.0f}s")

    async def _exit_master(self, host: str) -> bool:
        """Tear down a host's ControlMaster (`ssh -O exit`) and confirm it's gone.
        Returns True if the socket is gone, False if it may still be running or the
        teardown exceeded ``_EXIT_TIMEOUT`` (bounded so a wedged socket can't hang
        an in-progress connect/disconnect indefinitely)."""
        try:
            return await asyncio.wait_for(self._exit_master_inner(host),
                                          timeout=_EXIT_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("ssh: teardown of %s exceeded %.0fs — leaving it be",
                        host, _EXIT_TIMEOUT)
            return False

    async def _exit_master_inner(self, host: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-O", "exit", host,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        for _ in range(_DISCONNECT_POLL_ATTEMPTS):
            if not await self._check_master(host):
                return True
            await asyncio.sleep(0.5)
        return False

    @staticmethod
    async def _check_master(host: str) -> bool:
        return await SSHService._run_check(["ssh", "-O", "check", host])

    # -- operator approval window (Discord /approve) ------------------------

    async def _approve_listener(self) -> None:
        """Open a recovery window when the operator runs a Discord /approve.

        Subscribes to the social service's ``command`` emit (the same stream the
        2fa service arms bursts from) — local, or a peer's social@<peer> stream
        when AWM_SOCIAL_PEER is set. Owns its own reconnect/backoff; inert when
        no social service is present. This is the ONLY thing that lifts a breaker
        hold — there is no verb, so an agent cannot clear its own lock.
        """
        log.info("ssh: subscribing to social/command for operator /approve")
        backoff = 2.0
        while True:
            try:
                async for ev in gatewayclient.subscribe_maybe_peer(
                        gatewayclient.peer_env(_SOCIAL_PEER_ENV),
                        "social", "command"):
                    backoff = 2.0
                    self._handle_approve(ev)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — never let the task die
                log.debug("ssh: approval subscription dropped (retrying): %s", exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 30.0)

    def _handle_approve(self, ev: object) -> None:
        if not isinstance(ev, dict) or ev.get("command") != "approve":
            return
        device = str(ev.get("device") or "").strip()
        if not device:
            return
        # Only accept a device some managed host actually uses. An unknown string
        # (a typo, or another service's device) can never authorise an ssh recovery
        # anyway, and storing it would leak into _approve_until forever. Mirrors the
        # 2fa service, which validates the device before arming a burst.
        known = {c.twofa_device for c in KNOWN_HOSTS.values() if c.twofa_device}
        if device not in known:
            log.warning("ssh: /approve for unknown device %r — ignoring "
                        "(known: %s)", device, ", ".join(sorted(known)))
            return
        now = time.monotonic()
        # Opportunistically drop expired windows so the dict can't grow unbounded
        # over a long-lived process.
        for d in [d for d, until in self._approve_until.items() if until <= now]:
            self._approve_until.pop(d, None)
        self._approve_until[device] = now + _APPROVE_WINDOW_SECONDS
        log.info("ssh: operator /approve → recovery window open for device %r "
                 "(%.0fs)", device, _APPROVE_WINDOW_SECONDS)

    def _approve_active(self, device: str) -> bool:
        if not device:
            return False
        return self._approve_until.get(device, 0.0) > time.monotonic()

    # -- circuit breaker ----------------------------------------------------

    @staticmethod
    def _deviation_marker(cfg: HostConfig) -> str:
        return os.path.join(LIVE_DIR, f"{cfg.host}.askpass_deviation")

    @staticmethod
    def _safe_unlink(path: str) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("could not unlink %s: %s", path, e)

    @staticmethod
    def _read_lock(cfg: HostConfig) -> dict | None:
        try:
            with open(lock_path(cfg), "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            # A malformed/unreadable lock still means "locked" — fail safe.
            return {"reason": "lockfile present but unreadable"}

    def _write_lock(self, cfg: HostConfig, reason: str) -> None:
        os.makedirs(LOCK_DIR, exist_ok=True)
        payload = {
            "host": cfg.host,
            "reason": reason,
            "ts": time.time(),
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        with open(lock_path(cfg), "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def _clear_lock(self, cfg: HostConfig) -> None:
        self._safe_unlink(lock_path(cfg))

    def _failure_reason(self, cfg: HostConfig, marker: str, base: str) -> str:
        parts = [base]
        if os.path.exists(marker):
            parts.append("askpass deviation (unrecognized Duo prompt — "
                         "refused rather than guessing)")
        tail = self._read_stderr_tail(cfg)
        if tail:
            parts.append(f"ssh: {tail}")
        return "; ".join(parts)

    @staticmethod
    def _read_stderr_tail(cfg: HostConfig) -> str:
        """Notable lines from the captured ssh stderr, folded to one string."""
        try:
            with open(stderr_path(cfg), "r", encoding="utf-8",
                      errors="replace") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
        except FileNotFoundError:
            return ""
        notable = [ln for ln in lines
                   if any(s.lower() in ln.lower() for s in _NOTABLE_STDERR)]
        picked = notable or lines[-3:]
        return " | ".join(picked[-3:])

    def _lock_alert_text(self, cfg: HostConfig, reason: str) -> str:
        """The operator lockout page. Shared by the per-node breaker trip and the
        slot arbiter's LOCKED transition so both read identically."""
        device = cfg.twofa_device or "your device"
        return (
            f"🔒 awm-ssh held **{cfg.host}** after a failed connect — further "
            f"automated connects are refused so they can't burn an MFA attempt "
            f"toward provider lockout.\n"
            f"Reason: {reason}\n"
            f"To recover once you've checked it out: run `/approve {device}` in "
            f"Discord. While that window is open the service will reconnect on "
            f"its own.")

    async def _trip_breaker(self, cfg: HostConfig, reason: str) -> None:
        """Hold the host after a failed connect and page the operator. Threshold=1."""
        self._write_lock(cfg, reason)
        log.error("BREAKER TRIPPED — holding %s: %s", cfg.host, reason)
        await self._alert(self._lock_alert_text(cfg, reason))

    async def _send_social(self, text: str) -> Any:
        """Post ``text`` to Discord unimatrix0#notifications and RETURN the social
        send result. Touches no lockfile. The one federated notify wire, shared by
        the swallowing lock alert (:meth:`_alert`) and the result-surfacing
        self-test (:meth:`notify_test`) so both exercise the identical path."""
        return await gatewayclient.call_maybe_peer(
            gatewayclient.peer_env(_SOCIAL_PEER_ENV),
            "social", "send", {
                "account": _ALERT_ACCOUNT,
                "channel": _ALERT_CHANNEL,
                "text": text,
            })

    async def _alert(self, text: str) -> None:
        """Post to Discord unimatrix0#notifications. Never raises into connect."""
        try:
            await self._send_social(text)
        except Exception as e:
            log.error("failed to post lock alert to Discord: %s", e)

    async def notify_test(self, host: str = "[selftest]") -> dict:
        """Fire the Discord lock-alert wire on demand so the operator can confirm
        the ssh service can actually reach them BEFORE a real lockout depends on it.

        Sends through the EXACT :meth:`_send_social` path a real breaker trip uses
        (same peer selector, edge, bearer, account + channel), but writes NO
        lockfile and does NOT mutate breaker state, and — unlike :meth:`_alert` —
        does NOT swallow: the send result is surfaced so a broken notify wire fails
        loudly (that failure IS the signal). The message is unmistakably a test and
        carries no ``/approve`` instruction, so it can't train a reflex to clear a
        device."""
        peer = gatewayclient.peer_env(_SOCIAL_PEER_ENV)
        text = (
            f"🧪 awm-ssh notify self-test for **{host}** — the Discord alert wire "
            f"is working. No action needed; this is NOT a real lock."
        )
        result = await self._send_social(text)  # surfaces on failure — no try/except
        return {
            "sent": True,
            "peer": peer or "local",
            "account": _ALERT_ACCOUNT,
            "channel": _ALERT_CHANNEL,
            "result": result,
        }

    @staticmethod
    def _status_dict(cfg: HostConfig, status: str, *,
                     error: str = "", warning: str = "") -> dict:
        d: dict = {
            "host": cfg.host,
            "user": cfg.user,
            "port": cfg.port,
            "status": status,
        }
        if error:
            d["error"] = error
        if warning:
            d["warning"] = warning
        return d

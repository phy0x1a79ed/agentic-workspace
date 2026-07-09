from __future__ import annotations

import asyncio
import enum
import fcntl
import json
import logging
import os
import stat
import time
from dataclasses import dataclass

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

# Discord notifications target for lockout alerts: unimatrix0#notifications.
_ALERT_ACCOUNT = "discord-bot"
_ALERT_CHANNEL = "1522674357762261112"

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
            loop = asyncio.get_event_loop()
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
                log.info("ssh: reaping stale control socket %s", fn)
                self._safe_unlink(path)

    @staticmethod
    async def _check_socket(path: str) -> bool:
        """True iff a live master answers on the given control socket path."""
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-O", "check", "-S", path, "reap-probe",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        return proc.returncode == 0

    # -- state access -------------------------------------------------------

    def _host(self, name: str) -> HostState:
        hs = self._hosts.get(name)
        if hs is None:
            hs = HostState()
            self._hosts[name] = hs
        return hs

    # -- public verbs -------------------------------------------------------

    async def connect(self, host: str) -> dict:
        cfg = resolve_host(host)
        hs = self._host(host)

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
                                 "— clearing hold and reconnecting",
                                 cfg.host, cfg.twofa_device)
                        self._clear_lock(cfg)
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
        result = await self._do_connect(cfg)
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
        """Bring up the ControlMaster (vpn + 2fa + ssh + poll), bounded by an
        INTERNAL timeout. Any failure — including the timeout — trips the breaker
        and returns an error dict; success returns a connected dict. Never raises."""
        marker = self._deviation_marker(cfg)
        self._safe_unlink(marker)
        try:
            await asyncio.wait_for(self._attempt_master(cfg, marker),
                                   timeout=_CONNECT_TIMEOUT)
            log.info("connected to %s", cfg.host)
            return self._status_dict(cfg, "connected")
        except _AttemptFailed as e:
            reason = self._failure_reason(cfg, marker, str(e))
        except asyncio.TimeoutError:
            # Reap a possibly half-open master so the hung attempt can't linger.
            await self._exit_master(cfg.host)
            reason = self._failure_reason(
                cfg, marker, f"connect exceeded {_CONNECT_TIMEOUT:.0f}s")
        except Exception as e:  # noqa: BLE001
            log.error("connect to %s failed: %s", cfg.host, e)
            reason = self._failure_reason(cfg, marker, str(e))
        await self._trip_breaker(cfg, reason)
        # Neutral to the caller — the detailed reason goes to the lock + the
        # operator Discord alert, not to the agent.
        return self._status_dict(cfg, "error", error=f"connect to {cfg.host} failed")

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
            try:
                status = await gatewayclient.call(
                    "2fa", "status", {"device": cfg.twofa_device})
                burst_active = (
                    isinstance(status, dict)
                    and status.get("burst_active", False)
                )
            except Exception:
                burst_active = False

            if not burst_active:
                await gatewayclient.call(
                    "2fa", "burst", {
                        "device": cfg.twofa_device,
                        "window": 120,
                        "count": 1,
                    })
                log.info("2fa burst armed for %s on device %s",
                         cfg.host, cfg.twofa_device)
            else:
                log.info("2fa burst already active for device %s — reusing",
                         cfg.twofa_device)

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
        Returns True if the socket is gone, False if it may still be running."""
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
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-O", "check", host,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        return proc.returncode == 0

    # -- operator approval window (Discord /approve) ------------------------

    async def _approve_listener(self) -> None:
        """Open a recovery window when the operator runs a Discord /approve.

        Subscribes to the social service's ``command`` emit (the same stream the
        2fa service arms bursts from). Owns its own reconnect/backoff; inert when
        no social service is present. This is the ONLY thing that lifts a breaker
        hold — there is no verb, so an agent cannot clear its own lock.
        """
        log.info("ssh: subscribing to social/command for operator /approve")
        backoff = 2.0
        while True:
            try:
                async for ev in gatewayclient.subscribe("social", "command"):
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
        self._approve_until[device] = time.monotonic() + _APPROVE_WINDOW_SECONDS
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

    async def _trip_breaker(self, cfg: HostConfig, reason: str) -> None:
        """Hold the host after a failed connect and page the operator. Threshold=1."""
        self._write_lock(cfg, reason)
        log.error("BREAKER TRIPPED — holding %s: %s", cfg.host, reason)
        device = cfg.twofa_device or "your device"
        await self._alert(
            f"🔒 awm-ssh held **{cfg.host}** after a failed connect — further "
            f"automated connects are refused so they can't burn an MFA attempt "
            f"toward provider lockout.\n"
            f"Reason: {reason}\n"
            f"To recover once you've checked it out: run `/approve {device}` in "
            f"Discord. While that window is open the service will reconnect on "
            f"its own.")

    async def _alert(self, text: str) -> None:
        """Post to Discord unimatrix0#notifications. Never raises into connect."""
        try:
            await gatewayclient.call("social", "send", {
                "account": _ALERT_ACCOUNT,
                "channel": _ALERT_CHANNEL,
                "text": text,
            })
        except Exception as e:
            log.error("failed to post lock alert to Discord: %s", e)

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

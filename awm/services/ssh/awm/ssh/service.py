from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from awm import gatewayclient
from awm.ssh.config import (
    KNOWN_HOSTS,
    LIVE_DIR,
    LOCK_DIR,
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


class SSHService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._connecting: dict[str, asyncio.Task[dict]] = {}
        # device -> monotonic deadline until which an operator approval (a
        # Discord /approve on that device) is considered active. Recovery from a
        # tripped breaker happens ONLY inside such a window; there is no verb.
        self._approve_until: dict[str, float] = {}
        self._social_task: asyncio.Task | None = None

    def init(self) -> None:
        os.makedirs(LIVE_DIR, exist_ok=True)
        os.makedirs(LOCK_DIR, exist_ok=True)
        # Subscribe to the social service's slash commands so a Discord /approve
        # opens the recovery window (best-effort + self-reconnecting; inert when
        # no social service is present). Mirrors the 2fa service's listener.
        try:
            loop = asyncio.get_event_loop()
            self._social_task = loop.create_task(self._approve_listener())
        except RuntimeError as exc:  # no running loop (shouldn't happen at on_start)
            log.warning("ssh: approval listener not started: %s", exc)
        log.info("ssh service initialised (live_connections: %s)", LIVE_DIR)

    # -- public verbs -------------------------------------------------------

    async def connect(self, host: str) -> dict:
        cfg = resolve_host(host)

        # Circuit breaker: a host that already failed once is held BEFORE any
        # VPN/2FA/ssh, so it can never fire another MFA attempt and march toward
        # the provider's lockout ceiling. The hold lifts only while an operator
        # approval window (a Discord /approve on the host's device) is open —
        # there is no self-serve clear. Kept opaque to the caller on purpose.
        if self._read_lock(cfg) is not None:
            if not self._approve_active(cfg.twofa_device):
                return self._status_dict(
                    cfg, "unavailable",
                    error=f"{cfg.host} is not available for automated access "
                          f"right now")
            log.info("operator approval window open for %s (device %s) — "
                     "clearing hold and reconnecting", cfg.host, cfg.twofa_device)
            self._clear_lock(cfg)

        if await self._check_master(cfg.host):
            return self._status_dict(cfg, "connected")

        async with self._lock:
            if host in self._connecting:
                task = self._connecting[host]
            else:
                task = asyncio.create_task(self._do_connect(cfg))
                self._connecting[host] = task

        try:
            return await asyncio.wait_for(task, timeout=_CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            async with self._lock:
                self._connecting.pop(host, None)
            return self._status_dict(cfg, "error",
                                     error=f"connect timed out after {_CONNECT_TIMEOUT}s")

    async def disconnect(self, host: str) -> dict:
        cfg = resolve_host(host)

        async with self._lock:
            existing = self._connecting.pop(host, None)
        if existing is not None and not existing.done():
            existing.cancel()
            try:
                await existing
            except asyncio.CancelledError:
                pass

        proc = await asyncio.create_subprocess_exec(
            "ssh", "-O", "exit", cfg.host,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()

        for _ in range(_DISCONNECT_POLL_ATTEMPTS):
            if not await self._check_master(cfg.host):
                return self._status_dict(cfg, "disconnected")
            await asyncio.sleep(0.5)
        return self._status_dict(cfg, "disconnected",
                                 warning="master process may still be running")

    async def status(self) -> dict:
        hosts = list(KNOWN_HOSTS.items())
        coros = [self._check_master(cfg.host) for _name, cfg in hosts]
        results = await asyncio.gather(*coros)

        connections: dict[str, dict] = {}
        for (name, cfg), connected in zip(hosts, results):
            if connected:
                connections[name] = self._status_dict(cfg, "connected")
            elif self._read_lock(cfg) is not None:
                # Held by the breaker. Reported neutrally — no mechanism detail.
                connections[name] = self._status_dict(cfg, "unavailable")
            elif name in self._connecting and not self._connecting[name].done():
                connections[name] = self._status_dict(cfg, "connecting")
            else:
                connections[name] = self._status_dict(cfg, "disconnected")
        return {"connections": connections}

    # -- internal -----------------------------------------------------------

    async def _do_connect(self, cfg: HostConfig) -> dict:
        marker = self._deviation_marker(cfg)
        self._safe_unlink(marker)
        try:
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
                    log.info("connected to %s", cfg.host)
                    self._clear_lock(cfg)
                    return self._status_dict(cfg, "connected")
                await asyncio.sleep(_CHECK_POLL_INTERVAL)

            reason = self._failure_reason(
                cfg, marker,
                f"ControlMaster did not appear within "
                f"{_CHECK_POLL_ATTEMPTS * _CHECK_POLL_INTERVAL:.0f}s")
            await self._trip_breaker(cfg, reason)
            # Neutral to the caller — the detailed reason goes to the lock + the
            # operator Discord alert, not to the agent.
            return self._status_dict(cfg, "error",
                                     error=f"connect to {cfg.host} failed")

        except Exception as e:
            log.error("connect to %s failed: %s", cfg.host, e)
            reason = self._failure_reason(cfg, marker, str(e))
            await self._trip_breaker(cfg, reason)
            return self._status_dict(cfg, "error",
                                     error=f"connect to {cfg.host} failed")

        finally:
            async with self._lock:
                self._connecting.pop(cfg.host, None)

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

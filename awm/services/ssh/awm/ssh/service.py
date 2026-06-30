from __future__ import annotations

import asyncio
import logging
import os

from awm import gatewayclient
from awm.ssh.config import (
    KNOWN_HOSTS,
    LIVE_DIR,
    SSH_ASKPASS,
    HostConfig,
    resolve_host,
)

log = logging.getLogger("awm.ssh.service")

_CONNECT_TIMEOUT = 120.0
_CHECK_POLL_INTERVAL = 1.5
_CHECK_POLL_ATTEMPTS = 40
_DISCONNECT_POLL_ATTEMPTS = 8


class SSHService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._connecting: dict[str, asyncio.Task[dict]] = {}

    def init(self) -> None:
        os.makedirs(LIVE_DIR, exist_ok=True)
        log.info("ssh service initialised (live_connections: %s)", LIVE_DIR)

    # -- public verbs -------------------------------------------------------

    async def connect(self, host: str) -> dict:
        cfg = resolve_host(host)

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
            elif name in self._connecting and not self._connecting[name].done():
                connections[name] = self._status_dict(cfg, "connecting")
            else:
                connections[name] = self._status_dict(cfg, "disconnected")
        return {"connections": connections}

    # -- internal -----------------------------------------------------------

    async def _do_connect(self, cfg: HostConfig) -> dict:
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
            })

            proc = await asyncio.create_subprocess_exec(
                "ssh", "-f", "-N", "-M", cfg.host,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()

            for _ in range(_CHECK_POLL_ATTEMPTS):
                if await self._check_master(cfg.host):
                    log.info("connected to %s", cfg.host)
                    return self._status_dict(cfg, "connected")
                await asyncio.sleep(_CHECK_POLL_INTERVAL)

            return self._status_dict(
                cfg, "error",
                error=f"ControlMaster did not appear within "
                      f"{_CHECK_POLL_ATTEMPTS * _CHECK_POLL_INTERVAL:.0f}s")

        except Exception as e:
            log.error("connect to %s failed: %s", cfg.host, e)
            return self._status_dict(cfg, "error", error=str(e))

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

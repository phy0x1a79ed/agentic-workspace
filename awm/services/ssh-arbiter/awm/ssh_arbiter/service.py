"""The fleet's one login at a time per lockout-sensitive host.

Each host is IDLE, LEASED (an open lease session holds its one slot) or LOCKED (a
lockfile). A failed login locks the host, and only an operator ``/approve`` in
Discord lifts that. There is no verb for it, so an agent cannot clear its own hold.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from typing import Any

from awm import config, gatewayclient
from awm.ssh.config import AUTH_PHASE_STDERR, KNOWN_HOSTS, HostConfig, resolve_host

log = logging.getLogger("awm.ssh_arbiter.service")

_NODE_NAME = config.node_name()

LOCK_DIR = os.path.expanduser("~/locks")

# Which side a hold is attributable to, which decides what can lift it.
# _CAUSE_EXTERNAL is the default for everything, including an unrecognised cause,
# and only an operator /approve lifts it. _CAUSE_SELF means our own Duo approver
# could not function, so a fresh proof of its health lifts the hold. On 2026-07-26
# a hold on fir recorded "connect exceeded 120s" when nothing was wrong with fir.
_CAUSE_EXTERNAL = "external"
_CAUSE_SELF = "approver-unavailable"

# Kept at least as long as the 2fa social burst window, so the approver is still
# armed for the connect the approval authorizes.
_APPROVE_WINDOW_SECONDS = 300.0

# social's emit_probe command name. No operator can send it and no handler matches it.
_PROBE_COMMAND = "__awm_probe__"


def lock_path(cfg: HostConfig) -> str:
    return os.path.join(LOCK_DIR, f"{cfg.host}.lock")


class SSHArbiter:
    def __init__(self) -> None:
        self._leased: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._approve_until: dict[str, float] = {}
        self._approve_sub: Any = None
        self._probe_waiters: dict[str, asyncio.Future] = {}

    def init(self) -> None:
        os.makedirs(LOCK_DIR, exist_ok=True)
        gatewayclient.spawn_supervised("ssh-arbiter/approve-listener", self._approve_listener)
        log.info("ssh-arbiter initialised (locks: %s)", LOCK_DIR)

    # -- verbs --------------------------------------------------------------

    async def status(self) -> dict:
        now = time.monotonic()
        return {
            "holds": {name: self._read_lock(cfg) for name, cfg in KNOWN_HOSTS.items()
                      if self._read_lock(cfg) is not None},
            "leased": sorted(self._leased),
            "approval_windows": {d: int(until - now)
                                 for d, until in self._approve_until.items() if until > now},
            "subscription": self._subscription_health(),
        }

    def _subscription_health(self) -> dict:
        sub = self._approve_sub
        if sub is None:
            return {"healthy": False, "connected": False,
                    "last_error": "approve listener not started"}
        try:
            return sub.health()
        except Exception as exc:  # noqa: BLE001
            return {"healthy": False, "last_error": f"unavailable: {exc}"}

    # -- the slot -----------------------------------------------------------

    async def acquire(self, host: str) -> tuple[str, str | None]:
        """Return ``("granted", token)``, ``("busy", None)`` or ``("locked", reason)``."""
        cfg = resolve_host(host)
        async with self._lock:
            if host in self._leased:
                return ("busy", None)
            if self._read_lock(cfg) is not None:
                if self._approve_active(cfg.twofa_device):
                    # One-shot: each /approve authorises one attempt. A window left
                    # open would let a retrying caller re-clear its own hold and
                    # march to the provider's lockout.
                    self._approve_until.pop(cfg.twofa_device, None)
                    self._clear_lock(cfg)
                elif not await self._maybe_self_clear(cfg):
                    return ("locked", "held after a prior failed connect")
            token = secrets.token_urlsafe(8)
            self._leased[host] = token
            return ("granted", token)

    async def release(self, host: str, token: str, *, ok: bool, reason: str = "",
                      requester: str | None = None) -> None:
        """Free the slot, or lock the host and page the operator. Idempotent by ``token``.

        ``requester`` is the node that made the attempt, which is rarely this one.
        """
        cfg = resolve_host(host)
        reason = reason or "attempt failed"
        # Outside the lock: this may call 2fa, and the lock guards only the slot.
        cause = await self._classify_cause(cfg, reason)
        async with self._lock:
            if self._leased.get(host) != token:
                return
            del self._leased[host]
            if ok:
                self._clear_lock(cfg)
                return
            self._write_lock(cfg, reason, cause=cause)
        log.error("holding %s after a failed connect from %s: %s",
                  host, requester or "an unidentified node", reason)
        await self._alert(self._lock_alert_text(cfg, reason, requester=requester))

    async def lease_session(self, ctx: Any) -> None:
        """Hold one slot for the life of a connection.

        The open socket is the lease, so it has no expiry. A drop before a verdict
        locks the host, because the login may already have spent a Duo attempt.
        """
        init = ctx.init or {}
        host = init.get("host", "")
        requester = str(init.get("node") or "") or None
        bridge = await ctx.open_bridge()
        token: str | None = None
        try:
            try:
                resolve_host(host)
            except ValueError:
                await bridge.send(json.dumps({"lease": "error", "reason": f"unknown host {host!r}"}))
                return
            status, detail = await self.acquire(host)
            await bridge.send(json.dumps({"lease": "granted"} if status == "granted"
                                         else {"lease": status, "reason": detail or status}))
            if status != "granted":
                return
            token = detail
            verdict, reason = None, "requester dropped the lease without a verdict"
            try:
                async for raw in bridge:
                    msg = self._frame(raw)
                    if msg.get("verdict") in ("ok", "fail"):
                        verdict = msg["verdict"]
                        reason = msg.get("reason") or "requester reported connect failure"
                        requester = str(msg.get("node") or "") or requester
                        break
            except Exception:  # noqa: BLE001 — a dropped socket is a verdictless end
                pass
            held, token = token, None
            await self.release(host, held, ok=verdict == "ok", reason=reason, requester=requester)
        finally:
            if token is not None:
                await self.release(host, token, ok=False, reason="lease handler aborted",
                                   requester=requester)
            try:
                await bridge.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _frame(raw: Any) -> dict:
        if isinstance(raw, (bytes, bytearray)):
            return {}
        try:
            return json.loads(raw) or {}
        except json.JSONDecodeError:
            return {}

    # -- operator /approve --------------------------------------------------

    async def _approve_listener(self) -> None:
        self._approve_sub = gatewayclient.SupervisedSubscription(
            "ssh-arbiter/social.command", lambda: gatewayclient.subscribe("social", "command"),
            self._on_approve_event, intercept=self._intercept_probe)
        await self._approve_sub.run()

    async def _on_approve_event(self, ev: object) -> None:
        # The window opens before the receipt, so a Discord failure cannot close it.
        text = self._handle_approve(ev)
        if text is not None:
            await self._ack_approve(ev, text)

    def _handle_approve(self, ev: object) -> str | None:
        """Open a device's recovery window. Returns the receipt text, if the event is ours."""
        if not isinstance(ev, dict) or ev.get("command") != "approve":
            return None
        device = str(ev.get("device") or "").strip()
        if not device:
            return None
        known = {c.twofa_device for c in KNOWN_HOSTS.values() if c.twofa_device}
        if device not in known:
            log.warning("/approve for unknown device %r (known: %s)", device, ", ".join(sorted(known)))
            return f"⚠️ ssh: `{device}` is not a device any managed host uses (known: {', '.join(sorted(known))})"
        self._approve_until[device] = time.monotonic() + _APPROVE_WINDOW_SECONDS
        log.info("operator /approve: recovery window open for device %r", device)
        return (f"🔓 ssh [{_NODE_NAME}]: breaker recovery window open for `{device}` — "
                f"{int(_APPROVE_WINDOW_SECONDS // 60)} min")

    async def _ack_approve(self, ev: object, text: str) -> None:
        """Send the receipt. Silence after an /approve is how the operator sees a deaf arbiter."""
        account, channel = ev.get("account"), ev.get("channel_id")
        if not account or not channel:
            return
        try:
            await gatewayclient.call("social", "send",
                                     {"account": account, "channel": str(channel), "text": text})
        except Exception as exc:  # noqa: BLE001 — a receipt is not the window
            log.warning("/approve acknowledgement failed (the window is open regardless): %s", exc)

    def _approve_active(self, device: str) -> bool:
        return bool(device) and self._approve_until.get(device, 0.0) > time.monotonic()

    # -- holds --------------------------------------------------------------

    @staticmethod
    def _read_lock(cfg: HostConfig) -> dict | None:
        try:
            with open(lock_path(cfg), "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"reason": "lockfile present but unreadable"}

    @staticmethod
    def _write_lock(cfg: HostConfig, reason: str, *, cause: str = _CAUSE_EXTERNAL) -> None:
        os.makedirs(LOCK_DIR, exist_ok=True)
        path = lock_path(cfg)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"host": cfg.host, "reason": reason, "cause": cause, "ts": time.time(),
                       "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, f)
        os.replace(tmp, path)

    @staticmethod
    def _clear_lock(cfg: HostConfig) -> None:
        try:
            os.unlink(lock_path(cfg))
        except FileNotFoundError:
            pass

    async def _classify_cause(self, cfg: HostConfig, reason: str) -> str:
        """Attribute a failure to our own approver only on a timeout it cannot disprove.

        Ask the approver rather than infer from how long it has been quiet. On
        2026-09-01 a recency test filed a fir maintenance outage as ours, and the
        automatic retry that cause licenses spent a Duo push on a host that was off.
        """
        blob = reason.lower()
        if not cfg.twofa_device or any(s.lower() in blob for s in AUTH_PHASE_STDERR) or "exceeded" not in blob:
            return _CAUSE_EXTERNAL
        try:
            info = await gatewayclient.call("2fa", "ping", {"device": cfg.twofa_device})
        except Exception as exc:  # noqa: BLE001 — cannot ask, so cannot attribute
            log.info("cannot attribute %s's failure (2fa unreachable: %s)", cfg.host, exc)
            return _CAUSE_EXTERNAL
        return _CAUSE_EXTERNAL if (info or {}).get("ok") else _CAUSE_SELF

    async def _maybe_self_clear(self, cfg: HostConfig) -> bool:
        """Lift a self-inflicted hold on a live 2fa round-trip. Runs only on a request."""
        lock = self._read_lock(cfg)
        if lock is None or lock.get("cause") != _CAUSE_SELF or not cfg.twofa_device:
            return False
        try:
            res = await gatewayclient.call("2fa", "ping", {"device": cfg.twofa_device})
        except Exception as exc:  # noqa: BLE001
            log.info("%s stays held, could not verify the approver: %s", cfg.host, exc)
            return False
        if not (res or {}).get("reachable"):
            return False
        self._clear_lock(cfg)
        await self._alert(
            f"🔁 awm-ssh [{_NODE_NAME}] released its own hold on **{cfg.host}**.\n"
            f"The hold was recorded against our own Duo approver being unreachable "
            f"({lock.get('reason')}), and the approver has just verified a live round-trip. "
            f"One attempt is now permitted; a genuine failure will hold it again immediately.\n"
            f"No action needed.")
        return True

    # -- the operator wire --------------------------------------------------

    @staticmethod
    def _lock_alert_text(cfg: HostConfig, reason: str, *, requester: str | None = None) -> str:
        return (
            f"🔒 awm-ssh held **{cfg.host}** after a failed connect from "
            f"**{requester or 'an unidentified node'}** — further automated connects are refused "
            f"so they can't burn an MFA attempt toward provider lockout.\n"
            f"Reason: {reason}\n"
            f"To recover once you've checked it out: run `/approve {cfg.twofa_device or 'your device'}` "
            f"in Discord. While that window is open the next connect may proceed.")

    @staticmethod
    async def _send_social(text: str) -> Any:
        account, channel = config.operator_dm()
        return await gatewayclient.call("social", "send",
                                        {"account": account, "channel": channel, "text": text})

    async def _alert(self, text: str) -> None:
        try:
            await self._send_social(text)
        except Exception as e:  # noqa: BLE001 — a failed page must not change the hold
            log.error("failed to post lock alert to Discord: %s", e)

    async def notify_test(self, host: str = "[selftest]") -> dict:
        """Send a test page down the real alert wire. Failures surface."""
        result = await self._send_social(
            f"🧪 awm-ssh [{_NODE_NAME}] notify self-test for **{host}** — the Discord alert "
            f"wire is working. No action needed; this is NOT a real lock.")
        account, channel = config.operator_dm()
        return {"sent": True, "account": account, "channel": channel, "result": result}

    async def receive_test(self, timeout: float = 10.0) -> dict:
        """Prove an operator /approve can still reach this service. Opens no window."""
        if self._approve_sub is None:
            raise RuntimeError("the /approve subscription was never started")
        nonce = secrets.token_urlsafe(8)
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._probe_waiters[nonce] = waiter
        try:
            try:
                await gatewayclient.call("social", "emit_probe", {"nonce": nonce})
            except Exception as exc:  # noqa: BLE001 — not the same fault as being deaf
                raise RuntimeError(f"could not ask social to emit a probe ({exc}); "
                                   f"this says nothing about whether the arbiter can receive") from exc
            try:
                await asyncio.wait_for(waiter, timeout=timeout)
            except asyncio.TimeoutError:
                raise RuntimeError(f"probe emitted but not received within {timeout:.0f}s; "
                                   f"an operator /approve would not reach this service") from None
        finally:
            self._probe_waiters.pop(nonce, None)
        return {"received": True, "subscription": self._subscription_health()}

    def _intercept_probe(self, ev: object) -> bool:
        if not isinstance(ev, dict) or ev.get("command") != _PROBE_COMMAND:
            return False
        waiter = self._probe_waiters.get(str(ev.get("nonce") or ""))
        if waiter is not None and not waiter.done():
            waiter.set_result(True)
        return True

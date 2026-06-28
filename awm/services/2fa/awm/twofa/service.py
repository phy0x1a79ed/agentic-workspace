"""The 2fa service runtime — N independent Duo devices + approval engines.

Holds the state behind the ``2fa_*`` verbs. Each configured device is a
:class:`DeviceRuntime`: its own :class:`~awm.twofa.duo.DuoClient` (loaded lazily
from the device's on-disk creds), its own :class:`~awm.twofa.engine.ApprovalEngine`
(in-memory held/dedup state), and its own **burst** lifecycle. Devices are fully
independent — approving/denying or bursting one never touches another.

The verbs are deliberately thin wrappers over this object. Blocking Duo HTTP
calls always run off the event loop (sync verbs run in the adapter's worker
thread; the async burst loop uses ``asyncio.to_thread``), so the control WS is
never stalled.

Burst model — a per-device *expected-approval counter* plus a deadline that
overlapping bursts extend:

  * ``start_burst(device, count=1)`` adds ``count`` to ``rt.expected`` and pushes
    ``rt.burst_deadline`` to ``max(old, now+window)``; the first call spawns one
    poll task, later overlapping calls just bump the counter/deadline.
  * the poll loop runs while ``now < deadline`` **and** ``expected > 0``,
    auto-approving lone pushes and ticking ``expected`` down by each approval,
    ending early once every expected approval has landed.

Held state is in-memory and lost on respawn (hold-TTL is 120s by default), so
``2fa_approve <urgid> device=<name>`` only resolves a login still held in the
current process.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Config, DeviceCreds, paths_for
from .duo import DuoClient, Transaction
from .engine import ApprovalEngine
from .notify import NULL_NOTIFIER

log = logging.getLogger("awm.twofa.service")


def _tx_view(tx: Transaction) -> dict[str, Any]:
    return {"urgid": tx.urgid, "app": tx.app, "details": tx.details}


@dataclass
class DeviceRuntime:
    """Per-device state. Lazy ``client``/``engine``; burst counters mutated only
    on the event loop (see :meth:`TwoFAService.start_burst`)."""

    name: str
    creds: DeviceCreds
    client: DuoClient | None = None
    engine: ApprovalEngine | None = None
    load_error: str | None = None
    build_lock: threading.Lock = field(default_factory=threading.Lock)
    burst_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Burst state (event-loop only). deadline==0.0 means idle.
    burst_deadline: float = 0.0
    expected: int = 0
    burst_task: asyncio.Task | None = None
    last_burst: dict[str, Any] | None = None
    # Optional async callable(str) the burst posts progress to (per-approval +
    # window-end). Set by start_burst(notify=…); the social path uses it to
    # echo outcomes back to the Discord DM. None = silent.
    burst_notify: Any = None

    @property
    def enrolled(self) -> bool:
        return self.creds.enrolled

    def burst_active(self) -> bool:
        return self.burst_task is not None and not self.burst_task.done()


class TwoFAService:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or Config()
        self._devices: dict[str, DeviceRuntime] = {
            name: DeviceRuntime(name, creds)
            for name, creds in self.cfg.devices.items()
        }
        self._devices_lock = threading.Lock()
        self._social_task: asyncio.Task | None = None

    # ---- lifecycle --------------------------------------------------------

    def init(self) -> None:
        """on_start hook: ensure the runtime dir exists and warm enrolled devices.

        Best-effort — a device with no creds just stays un-enrolled until
        ``2fa_activate`` (or a creds copy) provisions it. Never raises.
        """
        try:
            from .config import SERVICE_DIR
            SERVICE_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("2fa: could not create runtime dir: %s", exc)
        enrolled = [rt for rt in self._devices.values() if rt.enrolled]
        if not enrolled:
            log.info("2fa: no device creds yet — run 2fa_activate or copy creds")
        for rt in enrolled:
            try:
                self._load_engine(rt)
                log.info("2fa: device %r enrolled (host=%s)", rt.name, rt.client.host)
            except Exception as exc:  # noqa: BLE001
                log.warning("2fa: device %r creds present but failed to load: %s",
                            rt.name, exc)
        # Subscribe to the social service's slash commands (Discord /approve →
        # arm a Duo burst). Best-effort + self-reconnecting, so it's harmless
        # when no social service is on the gateway. Spawned like social's own
        # connectors: a task on the already-running loop.
        if self.cfg.social_subscribe:
            try:
                loop = asyncio.get_event_loop()
                self._social_task = loop.create_task(self._social_listener())
            except RuntimeError as exc:  # no running loop (shouldn't happen at on_start)
                log.warning("2fa: social subscription not started: %s", exc)

    def _load_engine(self, rt: DeviceRuntime) -> ApprovalEngine:
        """Build (and cache) one device's client + engine from on-disk creds.

        Blocking (file read + RSA import). Guarded per-runtime so concurrent
        verb threads don't build it twice. Stores ``rt.load_error`` on failure.
        """
        with rt.build_lock:
            if rt.engine is not None:
                return rt.engine
            if not rt.enrolled:
                rt.load_error = (
                    f"2fa device {rt.name!r} not enrolled; copy creds to "
                    f"{rt.creds.creds_path.parent} or run 2fa_activate")
                raise RuntimeError(rt.load_error)
            try:
                client = DuoClient.load(rt.creds.creds_path, rt.creds.key_path)
                engine = ApprovalEngine(
                    client, NULL_NOTIFIER,
                    dedup_seconds=self.cfg.dedup_seconds,
                    approve_all_minutes=self.cfg.approve_all_minutes,
                    burst_threshold=self.cfg.burst_threshold,
                    hold_ttl_seconds=self.cfg.hold_ttl_seconds,
                )
            except Exception as exc:  # noqa: BLE001
                rt.load_error = str(exc)
                raise
            rt.client = client
            rt.engine = engine
            rt.load_error = None
            return engine

    # ---- social slash-command subscription --------------------------------

    async def _social_listener(self) -> None:
        """Listen to the social service's ``command`` emit and arm bursts.

        Owns its own reconnect/backoff (``subscribe`` yields one socket's worth
        of events then returns when it closes — it does NOT reconnect itself).
        Best-effort: when no ``social`` service is on the gateway the connect
        just fails and we retry with backoff, so this is inert until social
        shows up. Cancelled cleanly on shutdown.
        """
        from awm import gatewayclient

        log.info("2fa: subscribing to social/command for slash-armed bursts")
        backoff = 2.0
        while True:
            try:
                async for ev in gatewayclient.subscribe("social", "command"):
                    backoff = 2.0  # connected and receiving
                    await self._handle_social_command(ev)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — never let the task die
                log.debug("2fa: social subscription dropped (retrying): %s", exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 30.0)

    async def _handle_social_command(self, ev: Any) -> None:
        """React to one social ``command`` event. Only ``approve`` arms a burst."""
        if not isinstance(ev, dict) or ev.get("command") != "approve":
            return
        device = str(ev.get("device") or self.cfg.social_default_device).strip()
        rt = self._devices.get(device)
        if rt is None or not rt.enrolled:
            log.warning("2fa: social /approve for device %r — not enrolled; ignoring",
                        device)
            await self._social_reply(ev, f"⚠️ 2fa device `{device}` is not enrolled")
            return
        mins = int(self.cfg.social_window_seconds // 60) or 1
        log.info("2fa: social /approve → arming burst on %r (%.0fs window, %.0fs poll)",
                 rt.name, self.cfg.social_window_seconds, self.cfg.social_interval_seconds)

        async def _notify(text: str) -> None:
            # Echo burst progress (per-approval + window-end) back to the DM.
            await self._social_reply(ev, text)

        await self.start_burst(
            rt.name,
            window=self.cfg.social_window_seconds,
            interval=self.cfg.social_interval_seconds,
            count=self.cfg.social_burst_count,
            notify=_notify,
        )
        await self._social_reply(ev, f"✅ Duo approvals armed: `{rt.name}` — {mins} min")

    async def _social_reply(self, ev: Any, text: str) -> None:
        """Best-effort confirmation back to the originating DM channel."""
        account = (ev or {}).get("account")
        channel = (ev or {}).get("channel_id")
        if not account or not channel:
            return
        try:
            from awm import gatewayclient
            await gatewayclient.call(
                "social", "send",
                {"account": account, "channel": str(channel), "text": text})
        except Exception as exc:  # noqa: BLE001 — confirmation is non-critical
            log.debug("2fa: social reply failed: %s", exc)

    # ---- device resolution ------------------------------------------------

    def _require_device(self, device: str | None) -> DeviceRuntime:
        """Resolve a *required* device argument to its runtime, or raise."""
        if not device or not str(device).strip():
            raise ValueError(
                "device is required; one of: " + ", ".join(sorted(self._devices)))
        name = str(device).strip()
        rt = self._devices.get(name)
        if rt is None:
            raise ValueError(
                f"unknown device {name!r}; known: " + ", ".join(sorted(self._devices)))
        return rt

    def _target_devices(self, device: str | None) -> list[DeviceRuntime]:
        """Resolve an *optional* device for read verbs: one named, or all."""
        if device and str(device).strip():
            return [self._require_device(device)]
        return list(self._devices.values())

    def devices(self) -> dict[str, Any]:
        """List discovered devices and their enrollment — lets callers learn the
        valid ``device=`` names."""
        return {
            "devices": {
                rt.name: {"enrolled": rt.enrolled, "burst_active": rt.burst_active()}
                for rt in self._devices.values()
            },
            "default": _maybe_default(self._devices),
        }

    # ---- verbs ------------------------------------------------------------

    def _ping_one(self, rt: DeviceRuntime) -> dict[str, Any]:
        return {
            "enrolled": rt.enrolled,
            "host": rt.client.host if rt.client else None,
            "burst_active": rt.burst_active(),
        }

    def ping(self, device: str | None = None) -> dict[str, Any]:
        targets = self._target_devices(device)
        if device and str(device).strip():
            rt = targets[0]
            return {"ok": True, "device": rt.name, **self._ping_one(rt)}
        return {
            "ok": True,
            "devices": {rt.name: self._ping_one(rt) for rt in targets},
        }

    def _status_one(self, rt: DeviceRuntime) -> dict[str, Any]:
        out: dict[str, Any] = {
            "enrolled": rt.enrolled,
            "host": None,
            "burst_active": rt.burst_active(),
            "expected": rt.expected,
            "burst_remaining_seconds": _remaining(rt),
            "held": [],
            "held_count": 0,
            "approve_all_remaining_seconds": 0.0,
            "approved_count": 0,
            "last_burst": rt.last_burst,
        }
        if not rt.enrolled:
            return out
        try:
            engine = self._load_engine(rt)
        except Exception as exc:  # noqa: BLE001
            out["error"] = str(exc)
            return out
        held = engine.held_transactions()
        out.update(
            host=rt.client.host if rt.client else None,
            held=[_tx_view(t) for t in held],
            held_count=len(held),
            approve_all_remaining_seconds=round(engine.approve_all_remaining(), 1),
            approved_count=engine.approved_count,
        )
        return out

    def status(self, device: str | None = None) -> dict[str, Any]:
        targets = self._target_devices(device)
        if device and str(device).strip():
            rt = targets[0]
            return {"device": rt.name, **self._status_one(rt)}
        return {"devices": {rt.name: self._status_one(rt) for rt in targets}}

    def _pending_one(self, rt: DeviceRuntime) -> list[dict[str, Any]]:
        if not rt.enrolled:
            return []
        engine = self._load_engine(rt)
        return [_tx_view(t) for t in engine.held_transactions()]

    def pending(self, device: str | None = None) -> dict[str, Any]:
        targets = self._target_devices(device)
        if device and str(device).strip():
            rt = targets[0]
            held = self._pending_one(rt)
            return {"device": rt.name, "held": held, "held_count": len(held),
                    "enrolled": rt.enrolled}
        out: dict[str, Any] = {"devices": {}}
        for rt in targets:
            held = self._pending_one(rt)
            out["devices"][rt.name] = {
                "held": held, "held_count": len(held), "enrolled": rt.enrolled}
        return out

    def activate(self, code: str, device: str) -> dict[str, Any]:
        """Enroll a device from a ``CODE-BASE64HOST`` activation string.

        Writes ``paths_for(device)`` (mode 0600), (re)registers the runtime and
        rebuilds its engine. Blocking (network + RSA keygen) — runs in the
        adapter's worker thread.
        """
        if not code or not str(code).strip():
            raise ValueError("activation code is required")
        if not device or not str(device).strip():
            raise ValueError("device is required")
        name = str(device).strip()
        creds = paths_for(name)
        client = DuoClient()
        client.read_code(str(code).strip())
        client.activate()
        client.save(creds.creds_path, creds.key_path)
        # Register / reset the runtime so the next verb rebuilds from new creds.
        with self._devices_lock:
            rt = DeviceRuntime(name, creds)
            self._devices[name] = rt
        engine = self._load_engine(rt)
        return {"ok": True, "device": name, "host": client.host, "akey": client.akey,
                "enrolled": True, "approved_count": engine.approved_count}

    def approve(self, urgid: str, device: str) -> dict[str, Any]:
        if not urgid:
            raise ValueError("urgid is required")
        rt = self._require_device(device)
        engine = self._load_engine(rt)
        return {"ok": engine.approve(str(urgid), by="awm"), "device": rt.name,
                "urgid": str(urgid)}

    def deny(self, urgid: str, device: str) -> dict[str, Any]:
        if not urgid:
            raise ValueError("urgid is required")
        rt = self._require_device(device)
        engine = self._load_engine(rt)
        return {"ok": engine.deny(str(urgid), by="awm"), "device": rt.name,
                "urgid": str(urgid)}

    def approve_all(self, device: str) -> dict[str, Any]:
        rt = self._require_device(device)
        engine = self._load_engine(rt)
        cleared = engine.approve_all(by="awm")
        return {"ok": True, "device": rt.name, "cleared": cleared,
                "approve_all_remaining_seconds": round(engine.approve_all_remaining(), 1)}

    async def start_burst(self, device: str, window: float | None = None,
                          interval: float | None = None,
                          count: int = 1, notify: Any = None) -> dict[str, Any]:
        """Open (or extend) a counted burst on *device*.

        Adds ``count`` expected approvals and pushes the deadline to
        ``max(old, now+window)``. The first call spawns one poll task; overlapping
        calls reuse it. Returns ``started`` / ``extended`` immediately; polling
        happens in the background task. ``notify`` (an async ``callable(str)``)
        gets per-approval + window-end progress, if supplied.
        """
        rt = self._require_device(device)
        window = self.cfg.burst_window_seconds if window is None else float(window)
        interval = self.cfg.burst_interval_seconds if interval is None else float(interval)
        count = max(1, int(count))

        async with rt.burst_lock:
            engine = await asyncio.to_thread(self._load_engine, rt)
            was_active = rt.burst_active()
            rt.expected += count
            rt.burst_deadline = max(rt.burst_deadline, time.monotonic() + window)
            if notify is not None:
                rt.burst_notify = notify
            if not was_active:
                rt.burst_task = asyncio.create_task(self._run_burst(rt, engine, interval))
        return {
            "status": "extended" if was_active else "started",
            "device": rt.name,
            "expected": rt.expected,
            "interval": interval,
            "burst_remaining_seconds": _remaining(rt),
        }

    async def _run_burst(self, rt: DeviceRuntime, engine: ApprovalEngine,
                         interval: float) -> None:
        start_approved = engine.approved_count
        last_seen = start_approved
        notify = rt.burst_notify  # captured: the target that armed this window
        log.info("2fa burst[%s]: interval %.1fs, expected %d, window %.0fs",
                 rt.name, interval, rt.expected, _remaining(rt))
        try:
            while time.monotonic() < rt.burst_deadline and rt.expected > 0:
                try:
                    txs = await asyncio.to_thread(engine.client.get_transactions)
                    if txs:
                        log.info("2fa burst[%s]: %d pending transaction(s)",
                                 rt.name, len(txs))
                    await asyncio.to_thread(engine.handle_transactions, txs)
                except Exception as exc:  # noqa: BLE001
                    log.warning("2fa burst[%s]: get_transactions failed: %s",
                                rt.name, exc)
                # Any approval on this engine (auto or a concurrent manual one)
                # counts against the expected total.
                now_approved = engine.approved_count
                delta = now_approved - last_seen
                if delta > 0:
                    rt.expected = max(0, rt.expected - delta)
                    last_seen = now_approved
                    if notify is not None:
                        total = now_approved - start_approved
                        await self._safe_notify(
                            notify,
                            f"✅ Duo login approved on `{rt.name}` "
                            f"({total} this window)")
                if rt.expected <= 0:
                    break
                await asyncio.sleep(interval)
        finally:
            # Atomic cleanup (no await) so it's consistent vs. a concurrent
            # start_burst that re-armed the window mid-loop.
            approved = engine.approved_count - start_approved
            rt.last_burst = {"approved": approved,
                             "expected_remaining": max(0, rt.expected)}
            rt.expected = 0
            rt.burst_deadline = 0.0
            rt.burst_task = None
            rt.burst_notify = None
            log.info("2fa burst[%s]: window ended; approved %d login(s)",
                     rt.name, approved)
            if notify is not None:
                # Fire-and-forget so cleanup stays await-free (and consistent vs.
                # a concurrent re-arm). Summarises the window outcome.
                summary = (f"⌛ Approval window ended on `{rt.name}` — "
                           f"approved {approved} login(s)")
                asyncio.create_task(self._safe_notify(notify, summary))

    @staticmethod
    async def _safe_notify(notify: Any, text: str) -> None:
        """Call a burst notifier, swallowing any error (progress is best-effort)."""
        try:
            await notify(text)
        except Exception as exc:  # noqa: BLE001
            log.debug("2fa burst notify failed: %s", exc)


def _remaining(rt: DeviceRuntime) -> float:
    if rt.burst_deadline <= 0.0:
        return 0.0
    return round(max(0.0, rt.burst_deadline - time.monotonic()), 1)


def _maybe_default(devices: dict[str, DeviceRuntime]) -> str | None:
    """The implicit default device name, if exactly one is enrolled."""
    enrolled = [n for n, rt in devices.items() if rt.enrolled]
    return enrolled[0] if len(enrolled) == 1 else None

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

Burst model — a per-device *expected-approval budget* (held in the engine) plus a
deadline that overlapping bursts extend:

  * ``start_burst(device, count=1)`` calls ``engine.grant(count)`` and pushes
    ``rt.burst_deadline`` to ``max(old, now+window)``; the first call spawns one
    poll task, later overlapping calls just add budget / extend the deadline.
  * the poll loop runs while ``now < deadline`` **and** ``engine.budget_remaining()
    > 0``; the engine approves pending pushes up to the budget (oldest-first) and
    decrements as it does, so N overlapping connects approve N overlapping pushes.
    Teardown calls ``engine.clear_budget()`` so leftover authorization never
    outlives the window.

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

# Singleton re-homing selector (federation). 2fa is canonical on one node; on
# that node social is co-located so this is unset and social calls stay local.
# A node that borrows social exports AWM_SOCIAL_PEER=<peer>. Read fresh per use.
_SOCIAL_PEER_ENV = "AWM_SOCIAL_PEER"
# Duo reachability probe budget. Short: `ping` is called on the recovery path,
# where a slow answer is nearly as bad as a wrong one.
_REACH_PROBE_TIMEOUT = 8


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
    # Burst state (event-loop only). deadline==0.0 means idle. The expected-
    # approval budget lives in the engine (ApprovalEngine.grant/budget_remaining),
    # so the decision and the counter share one lock.
    burst_deadline: float = 0.0
    burst_task: asyncio.Task | None = None
    last_burst: dict[str, Any] | None = None
    # Optional async callable(str) the burst posts progress to (per-approval +
    # window-end). Set by start_burst(notify=…); the social path uses it to
    # echo outcomes back to the Discord DM. None = silent.
    burst_notify: Any = None
    # Wall-clock of the last VERIFIED round-trip to the Duo API for this
    # device. Recency, not the absence of a recorded error, is what any
    # downstream consumer must require — see TwoFAService.ping.
    last_reachable_ts: float | None = None
    last_reach_error: str | None = None
    # Monotonically increasing count of Duo transactions this process has
    # OBSERVED for the device, across every burst window. Never reset while the
    # process lives, which is what lets a caller take a reading before it does
    # something that may fire a push and compare afterwards: equal readings mean
    # Duo saw nothing in between. A restart zeroes it, and a reading that went
    # backwards must therefore be read as "no evidence", never as zero.
    transactions_seen: int = 0

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
        # The live social/command subscription, once started. Read by `ping`
        # so a deaf listener is visible before an emergency needs it.
        self._social_sub: Any = None

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
                from awm import gatewayclient
                self._social_task = gatewayclient.spawn_supervised(
                    "2fa/social-listener", self._social_listener)
            except RuntimeError as exc:  # no running loop (shouldn't happen at on_start)
                log.error("2fa: social subscription not started — Discord "
                          "/approve will not arm a burst: %s", exc)

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

        Reconnect, backoff, and the idle-deadline re-subscribe belong to
        :class:`SupervisedSubscription` (``subscribe`` yields one socket's
        worth of events then returns when it closes — it does NOT reconnect
        itself). Best-effort: when no ``social`` service is on the gateway the
        connect just fails and we retry with backoff, so this is inert until
        social shows up. Cancelled cleanly on shutdown.
        """
        from awm import gatewayclient

        def _stream():
            # peer selector read per connection, so a re-home takes effect on
            # the next reconnect without a restart.
            return gatewayclient.subscribe_maybe_peer(
                gatewayclient.peer_env(_SOCIAL_PEER_ENV), "social", "command")

        self._social_sub = gatewayclient.SupervisedSubscription(
            "2fa/social.command", _stream, self._handle_social_command)
        await self._social_sub.run()

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
            await gatewayclient.call_maybe_peer(
                gatewayclient.peer_env(_SOCIAL_PEER_ENV),
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

    def _probe_one(self, rt: DeviceRuntime) -> dict[str, Any]:
        """One device's reachability, via a real read-only Duo API call.

        ``get_transactions`` is a GET that lists already-pending pushes: it
        fires nothing, costs no multi-factor budget, and is the same call the
        approval engine polls with — so if it works, the approver works.

        This exists because ``ping`` used to report ``ok: true, enrolled:
        true`` purely from local enrollment state, with no network call at
        all. On 2026-07-26 it said exactly that while the Duo API was
        unresolvable and every push was timing out. That false green is what
        made the approver look healthy throughout the diagnosis, so nothing
        downstream may depend on this answer until it is a real one.
        """
        out: dict[str, Any] = {
            "enrolled": rt.enrolled,
            "host": rt.client.host if rt.client else None,
            "burst_active": rt.burst_active(),
            "reachable": False,
            "last_reachable_at": rt.last_reachable_ts,
        }
        if not rt.enrolled:
            out["error"] = "device not enrolled"
            return out
        try:
            engine = self._load_engine(rt)
            engine.client.get_transactions(timeout=_REACH_PROBE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — an unreachable API IS the answer
            rt.last_reach_error = f"{type(exc).__name__}: {exc}"
            out["error"] = rt.last_reach_error
            out["host"] = rt.client.host if rt.client else None
            return out
        rt.last_reachable_ts = time.time()
        rt.last_reach_error = None
        out.update(reachable=True, host=rt.client.host if rt.client else None,
                   last_reachable_at=rt.last_reachable_ts)
        return out

    async def ping(self, device: str | None = None) -> dict[str, Any]:
        """Reachability of the Duo API, per device. ``ok`` means *verified*.

        The probe is blocking (``requests``), so it runs off the event loop.
        """
        targets = self._target_devices(device)
        probes = {rt.name: await asyncio.to_thread(self._probe_one, rt)
                  for rt in targets}
        ok = bool(probes) and all(p.get("reachable") for p in probes.values())
        if device and str(device).strip():
            rt = targets[0]
            return {"ok": ok, "device": rt.name, **probes[rt.name]}
        return {"ok": ok, "devices": probes}

    def reachability(self, device: str) -> dict[str, Any]:
        """The last VERIFIED reachability for one device, without probing.

        Read by the ssh arbiter's self-clear check. Deliberately returns the
        recorded timestamp rather than a fresh probe result, so the caller
        decides what counts as recent — "no error seen" must never be able to
        stand in for "reachable".
        """
        rt = self._devices.get(str(device or "").strip())
        if rt is None:
            return {"known": False, "last_reachable_at": None}
        return {
            "known": True,
            "enrolled": rt.enrolled,
            "last_reachable_at": rt.last_reachable_ts,
            "last_error": rt.last_reach_error,
        }

    def _status_one(self, rt: DeviceRuntime) -> dict[str, Any]:
        out: dict[str, Any] = {
            "enrolled": rt.enrolled,
            "host": None,
            "burst_active": rt.burst_active(),
            "expected": 0,
            "burst_remaining_seconds": _remaining(rt),
            "held": [],
            "held_count": 0,
            "approve_all_remaining_seconds": 0.0,
            "approved_count": 0,
            "last_burst": rt.last_burst,
            # Duo transactions observed for this device over the life of this
            # process. Only its DIFFERENCE against a reading taken earlier means
            # anything — see start_burst's transactions_seen. Set on the base
            # dict so an unenrolled device still answers the question.
            "transactions_seen": rt.transactions_seen,
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
            expected=engine.budget_remaining(),
            held=[_tx_view(t) for t in held],
            held_count=len(held),
            approve_all_remaining_seconds=round(engine.approve_all_remaining(), 1),
            approved_count=engine.approved_count,
        )
        return out

    def _subscription_health(self) -> dict[str, Any]:
        """Health of the social/command wire that carries ``/approve``.

        A deaf listener used to be indistinguishable from an idle one. Never
        raises: health has to be reportable precisely when things are broken.
        """
        sub = self._social_sub
        if sub is None:
            if not self.cfg.social_subscribe:
                # Say *why* it is off. "Disabled" on the node that borrows 2fa
                # is the correct state, and an operator who cannot tell that
                # from a fault will go looking for a bug that isn't there.
                from awm import gatewayclient
                owner = gatewayclient.peer_env("AWM_TWOFA_PEER")
                why = (f"2fa is owned by peer {owner!r}; only the owner listens "
                       f"for /approve" if owner else
                       "social subscription disabled by config")
                return {"healthy": True, "connected": False,
                        "listening": False, "owner": owner,
                        "last_error": why}
            return {"healthy": False, "connected": False,
                    "last_error": "social listener not started"}
        try:
            return sub.health()
        except Exception as exc:  # noqa: BLE001
            return {"healthy": False, "last_error": f"unavailable: {exc}"}

    def status(self, device: str | None = None) -> dict[str, Any]:
        targets = self._target_devices(device)
        sub = self._subscription_health()
        if device and str(device).strip():
            rt = targets[0]
            return {"device": rt.name, **self._status_one(rt),
                    "subscription": sub}
        return {"devices": {rt.name: self._status_one(rt) for rt in targets},
                "subscription": sub}

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
            # grant() is a read-modify-write under the engine lock (which can be
            # held across a Duo reply), so run it off the event loop.
            budget = await asyncio.to_thread(engine.grant, count)
            rt.burst_deadline = max(rt.burst_deadline, time.monotonic() + window)
            if notify is not None:
                rt.burst_notify = notify
            # Decide started-vs-extended by whether a *live* task exists — checked
            # here under burst_lock (after the awaits), NOT sampled before them.
            # _run_burst does its teardown under this same lock and re-checks for a
            # re-arm, so: if a live task exists it is guaranteed to observe this
            # grant/deadline (it will re-loop rather than tear down); if the prior
            # task already tore down (task None/done) we spawn a fresh one. This
            # closes the was_active TOCTOU that could strand a grant with no poller.
            live = rt.burst_task is not None and not rt.burst_task.done()
            # The observation baseline. What makes it sound is that arming
            # PRECEDES the caller's login — its own push can never already be in
            # this number, so an unchanged count later cannot be hiding it. A
            # concurrent sibling on the same device can land inside the window
            # and inflate the later reading, which produces a spurious hold: the
            # harmless direction.
            seen_at_arm = rt.transactions_seen
            if not live:
                rt.burst_task = asyncio.create_task(self._run_burst(rt, engine, interval))
        return {
            "status": "extended" if live else "started",
            "device": rt.name,
            "expected": budget,
            "interval": interval,
            "burst_remaining_seconds": _remaining(rt),
            # The observation counter as of arming. A caller that is about to do
            # something which may fire a push keeps this and compares it against
            # the same field on `status` afterwards: an unchanged reading means
            # Duo saw no transaction on this device in between, which is the only
            # positive evidence that no MFA attempt was spent.
            "transactions_seen": seen_at_arm,
        }

    async def _run_burst(self, rt: DeviceRuntime, engine: ApprovalEngine,
                         interval: float) -> None:
        start_approved = engine.approved_count
        start_seen = rt.transactions_seen
        last_seen = start_approved
        notify = rt.burst_notify  # captured: the target that armed this window
        log.info("2fa burst[%s]: interval %.1fs, budget %d, window %.0fs",
                 rt.name, interval, engine.budget_remaining(), _remaining(rt))
        approved = 0
        observed = 0
        final_notify = notify
        torn_down = False
        try:
            # Outer loop: the poll loop runs until deadline/budget, then a
            # teardown-or-continue decision made ATOMICALLY under burst_lock. If a
            # concurrent start_burst re-armed the window (extended the deadline or
            # added budget) during our exit, we resume polling instead of tearing
            # down — so a grant made while we were exiting is never stranded.
            while True:
                try:
                    while (time.monotonic() < rt.burst_deadline
                           and engine.budget_remaining() > 0):
                        try:
                            txs = await asyncio.to_thread(engine.client.get_transactions)
                            # This call IS a verified Duo round-trip, so record it
                            # as one. Before this the timestamp moved only when
                            # somebody called ping, which made it decay with idle
                            # time alone — and a consumer requiring recency then
                            # read a quiet approver as a broken one (2026-09-01: an
                            # ssh hold on a host that was simply under maintenance
                            # was filed as "approver-unavailable" on that basis).
                            rt.last_reachable_ts = time.time()
                            rt.last_reach_error = None
                            if txs:
                                rt.transactions_seen += len(txs)
                                log.info("2fa burst[%s]: %d pending transaction(s)",
                                         rt.name, len(txs))
                            await asyncio.to_thread(engine.handle_transactions, txs)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("2fa burst[%s]: get_transactions failed: %s",
                                        rt.name, exc)
                        # The engine owns the budget and decrements it as it
                        # approves; here we only surface per-approval progress (auto
                        # or a concurrent manual approval both bump approved_count).
                        now_approved = engine.approved_count
                        delta = now_approved - last_seen
                        if delta > 0:
                            last_seen = now_approved
                            notify = rt.burst_notify  # re-read: a re-arm may reset it
                            if notify is not None:
                                total = now_approved - start_approved
                                await self._safe_notify(
                                    notify,
                                    f"✅ Duo login approved on `{rt.name}` "
                                    f"({total} this window)")
                        if engine.budget_remaining() <= 0:
                            break
                        await asyncio.sleep(interval)
                except Exception as exc:  # noqa: BLE001 — never exit without teardown
                    log.warning("2fa burst[%s]: poll loop error: %s", rt.name, exc)

                async with rt.burst_lock:
                    if (time.monotonic() < rt.burst_deadline
                            and engine.budget_remaining() > 0):
                        # Re-armed while we were exiting — keep going.
                        notify = rt.burst_notify
                        continue
                    # Genuinely done. Tear down atomically WHILE holding burst_lock,
                    # so a concurrent start_burst cannot grant into a window we are
                    # closing (it will instead see no live task and spawn a fresh
                    # one). clear_budget() zeros any leftover authorization so a
                    # stray push outside a window can't be auto-approved later.
                    approved = engine.approved_count - start_approved
                    remaining = engine.clear_budget()
                    observed = rt.transactions_seen - start_seen
                    rt.last_burst = {"approved": approved,
                                     "expected_remaining": remaining,
                                     "observed": observed}
                    rt.burst_deadline = 0.0
                    rt.burst_task = None
                    rt.burst_notify = None
                    final_notify = notify
                    torn_down = True
                    break
        finally:
            if not torn_down:
                # Abnormal exit (e.g. cancellation). Clear budget and detach so no
                # authorization outlives the window and a re-arm can spawn afresh.
                approved = engine.approved_count - start_approved
                observed = rt.transactions_seen - start_seen
                engine.clear_budget()
                rt.burst_deadline = 0.0
                if rt.burst_task is asyncio.current_task():
                    rt.burst_task = None
                rt.burst_notify = None
        # "approved 0" alone is ambiguous — it reads the same whether Duo never
        # heard from the login or heard and we declined. The observed count
        # separates them, and is the line a later diagnosis needs.
        log.info("2fa burst[%s]: window ended; approved %d login(s), "
                 "observed %d transaction(s)", rt.name, approved, observed)
        if final_notify is not None:
            # Fire-and-forget so the summary can't stall a concurrent re-arm.
            summary = (f"⌛ Approval window ended on `{rt.name}` — "
                       f"approved {approved} login(s)")
            asyncio.create_task(self._safe_notify(final_notify, summary))

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

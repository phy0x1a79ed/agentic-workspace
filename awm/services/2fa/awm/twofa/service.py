"""The 2fa service runtime — one long-lived Duo device + approval engine.

Holds the singleton state behind the ``2fa_*`` verbs:

  * a :class:`~awm.twofa.duo.DuoClient` loaded from the on-disk device creds,
  * the :class:`~awm.twofa.engine.ApprovalEngine` (in-memory held/dedup state),
  * an on-demand background **burst** poll task.

The verbs are deliberately thin wrappers over this object. Blocking Duo HTTP
calls are always run off the event loop (the sync verbs run in the adapter's
worker thread; the async burst loop uses ``asyncio.to_thread``), so the control
WS is never stalled.

Held state is in-memory and lost on respawn — the same as the original mira
daemon (hold-TTL is 120s by default), so ``2fa_approve <urgid>`` only resolves a
login still held in the current process.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from .config import Config
from .duo import DuoClient, Transaction
from .engine import ApprovalEngine
from .notify import NULL_NOTIFIER

log = logging.getLogger("awm.twofa.service")


def _tx_view(tx: Transaction) -> dict[str, Any]:
    return {"urgid": tx.urgid, "app": tx.app, "details": tx.details}


class TwoFAService:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or Config()
        self._client: DuoClient | None = None
        self._engine: ApprovalEngine | None = None
        self._build_lock = threading.Lock()
        self._burst_lock = asyncio.Lock()
        self._burst_task: asyncio.Task | None = None
        self._last_burst: dict[str, Any] | None = None

    # ---- lifecycle --------------------------------------------------------

    def init(self) -> None:
        """on_start hook: ensure the runtime dir exists and warm the engine.

        Best-effort — a missing device just leaves the service un-enrolled until
        ``2fa_activate`` (or a creds copy) provisions it. Never raises.
        """
        try:
            self.cfg.creds_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("2fa: could not create runtime dir: %s", exc)
        if self.cfg.enrolled:
            try:
                self._load_engine()
                log.info("2fa: device enrolled (host=%s)", self._client.host)
            except Exception as exc:  # noqa: BLE001
                log.warning("2fa: device creds present but failed to load: %s", exc)
        else:
            log.info("2fa: no device creds yet — run 2fa_activate or copy creds")

    def _load_engine(self) -> ApprovalEngine:
        """Build (and cache) the client + engine from on-disk creds.

        Blocking (file read + RSA import). Guarded so concurrent verb threads
        don't build it twice.
        """
        with self._build_lock:
            if self._engine is not None:
                return self._engine
            if not self.cfg.enrolled:
                raise RuntimeError(
                    "2fa device not enrolled; copy creds to "
                    f"{self.cfg.creds_path.parent} or run 2fa_activate")
            client = DuoClient.load(self.cfg.creds_path, self.cfg.key_path)
            engine = ApprovalEngine(
                client, NULL_NOTIFIER,
                dedup_seconds=self.cfg.dedup_seconds,
                approve_all_minutes=self.cfg.approve_all_minutes,
                burst_threshold=self.cfg.burst_threshold,
                hold_ttl_seconds=self.cfg.hold_ttl_seconds,
            )
            self._client = client
            self._engine = engine
            return engine

    def _burst_active(self) -> bool:
        return self._burst_task is not None and not self._burst_task.done()

    # ---- verbs ------------------------------------------------------------

    def ping(self) -> dict[str, Any]:
        return {
            "ok": True,
            "enrolled": self.cfg.enrolled,
            "host": self._client.host if self._client else None,
            "burst_active": self._burst_active(),
        }

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "enrolled": self.cfg.enrolled,
            "host": None,
            "burst_active": self._burst_active(),
            "held": [],
            "held_count": 0,
            "approve_all_remaining_seconds": 0.0,
            "approved_count": 0,
            "last_burst": self._last_burst,
        }
        if not self.cfg.enrolled:
            return out
        try:
            engine = self._load_engine()
        except Exception as exc:  # noqa: BLE001
            out["error"] = str(exc)
            return out
        held = engine.held_transactions()
        out.update(
            host=self._client.host if self._client else None,
            held=[_tx_view(t) for t in held],
            held_count=len(held),
            approve_all_remaining_seconds=round(engine.approve_all_remaining(), 1),
            approved_count=engine.approved_count,
        )
        return out

    def pending(self) -> dict[str, Any]:
        if not self.cfg.enrolled:
            return {"held": [], "held_count": 0, "enrolled": False}
        engine = self._load_engine()
        held = engine.held_transactions()
        return {"held": [_tx_view(t) for t in held], "held_count": len(held),
                "enrolled": True}

    def activate(self, code: str) -> dict[str, Any]:
        """Enroll a fresh device from a ``CODE-BASE64HOST`` activation string.

        Writes ``creds.json`` + ``device_key.pem`` (mode 0600) and rebuilds the
        engine. Blocking (network + RSA keygen) — runs in the adapter's worker
        thread.
        """
        if not code or not str(code).strip():
            raise ValueError("activation code is required")
        client = DuoClient()
        client.read_code(str(code).strip())
        client.activate()
        client.save(self.cfg.creds_path, self.cfg.key_path)
        # Drop any stale engine so the next verb rebuilds from the new device.
        with self._build_lock:
            self._client = None
            self._engine = None
        engine = self._load_engine()
        return {"ok": True, "host": client.host, "akey": client.akey,
                "enrolled": True, "approved_count": engine.approved_count}

    def approve(self, urgid: str) -> dict[str, Any]:
        if not urgid:
            raise ValueError("urgid is required")
        engine = self._load_engine()
        return {"ok": engine.approve(str(urgid), by="awm"), "urgid": str(urgid)}

    def deny(self, urgid: str) -> dict[str, Any]:
        if not urgid:
            raise ValueError("urgid is required")
        engine = self._load_engine()
        return {"ok": engine.deny(str(urgid), by="awm"), "urgid": str(urgid)}

    def approve_all(self) -> dict[str, Any]:
        engine = self._load_engine()
        cleared = engine.approve_all(by="awm")
        return {"ok": True, "cleared": cleared,
                "approve_all_remaining_seconds": round(engine.approve_all_remaining(), 1)}

    async def start_burst(self, window: float | None = None,
                          interval: float | None = None,
                          exit_on_approve: bool | None = None) -> dict[str, Any]:
        """Open a bounded poll window that auto-approves a single login and holds
        a burst — the awm analogue of mira's ``POST /burst``.

        Returns immediately (``started`` / ``already-running``); the polling
        happens in a background task.
        """
        window = self.cfg.burst_window_seconds if window is None else float(window)
        interval = self.cfg.burst_interval_seconds if interval is None else float(interval)
        if exit_on_approve is None:
            exit_on_approve = self.cfg.burst_exit_on_approve

        async with self._burst_lock:
            if self._burst_active():
                return {"status": "already-running"}
            # Ensure enrolled before claiming "started".
            engine = await asyncio.to_thread(self._load_engine)
            self._burst_task = asyncio.create_task(
                self._run_burst(engine, window, interval, bool(exit_on_approve)))
        return {"status": "started", "window": window, "interval": interval,
                "exit_on_approve": bool(exit_on_approve)}

    async def _run_burst(self, engine: ApprovalEngine, window: float,
                         interval: float, exit_on_approve: bool) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + window
        start_approved = engine.approved_count
        log.info("2fa burst: interval %.1fs, window %.0fs, exit_on_approve=%s",
                 interval, window, exit_on_approve)
        try:
            while loop.time() < deadline:
                try:
                    txs = await asyncio.to_thread(engine.client.get_transactions)
                    if txs:
                        log.info("2fa burst: %d pending transaction(s)", len(txs))
                    await asyncio.to_thread(engine.handle_transactions, txs)
                except Exception as exc:  # noqa: BLE001
                    log.warning("2fa burst: get_transactions failed: %s", exc)
                if exit_on_approve and engine.approved_count > start_approved:
                    log.info("2fa burst: approved a login; exiting early")
                    break
                await asyncio.sleep(interval)
        finally:
            approved = engine.approved_count - start_approved
            self._last_burst = {"approved": approved, "window": window}
            log.info("2fa burst: window ended; approved %d login(s)", approved)

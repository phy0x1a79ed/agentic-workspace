"""Approval engine: decides what to do with each pending Duo transaction.

Ported verbatim from the ``virtual-auth`` project; the only change is that the
notifier is the no-op :class:`awm.twofa.notify.Notifier` instead of a Discord
poster (the awm verb surface replaces the Discord buttons).

Policy:

  * an armed burst window carries an **expected-approval budget** (``grant(n)``);
    each auto-approval consumes one. While budget remains, pending logins are
    auto-approved oldest-first — held ones (longest-waiting) before newly-seen.
  * pushes **beyond the budget** -> hold + notify with Approve/Deny buttons;
    never auto-approved. A later ``grant`` retro-promotes the oldest held first.
  * **approve-all window** (opened by a control command) -> approve everything,
               held and incoming, for ``approve_all_minutes`` (does NOT consume
               the counted budget).
  * duplicate pushes for the same urgid within ``dedup_seconds`` are suppressed,
    and we never answer the same urgid twice.

The budget is the authorization the ssh service (or a Discord ``/approve``) arms
the window with: N overlapping connects -> ``grant`` totals N -> N overlapping
pushes all approve. It replaces the old ``len(txs) > threshold -> hold every
one`` heuristic, which contradicted the armed count and held legitimate logins.

The burst poll loop calls :meth:`handle_transactions` after every fetch; the
control surface (the ``2fa_*`` verbs) calls :meth:`approve` / :meth:`deny` /
:meth:`approve_all`. All held/acted/budget state is guarded by a single lock.
Note :meth:`_answer` holds that lock across the Duo HTTP reply, so event-loop
callers must reach budget only via :meth:`grant` (run off-loop) or the lock-free
:meth:`budget_remaining` / :meth:`clear_budget` int read/write.
"""

from __future__ import annotations

import logging
import threading
import time

from .duo import DuoClient, Transaction
from .notify import Notifier

log = logging.getLogger("awm.twofa.engine")


class ApprovalEngine:
    def __init__(self, client: DuoClient, notifier: Notifier, *,
                 dedup_seconds: float = 3.0, approve_all_minutes: float = 5.0,
                 hold_ttl_seconds: float = 120.0):
        self.client = client
        self.notifier = notifier
        self.dedup_seconds = dedup_seconds
        self.approve_all_seconds = approve_all_minutes * 60.0
        self.hold_ttl_seconds = hold_ttl_seconds

        self._lock = threading.Lock()
        self._held: dict[str, tuple[Transaction, float]] = {}  # urgid -> (tx, added_at)
        self._acted: dict[str, float] = {}  # urgid -> answered_at (dedup / no double-answer)
        self._approve_all_until: float = 0.0
        # Remaining auto-approvals authorized by armed burst windows. grant() adds
        # (off-loop), the budget branch of handle_transactions decrements under the
        # lock, clear_budget() zeros it when a window ends. A plain int so
        # budget_remaining() is a lock-free atomic read (see class docstring).
        self._budget: int = 0
        # Count of successful *approvals* sent to Duo. The burst loop reads this to
        # report per-approval progress.
        self.approved_count: int = 0

    # ---- budget (armed-window authorization) ------------------------------

    def grant(self, n: int) -> int:
        """Add ``n`` auto-approvals to the budget; returns the new total.

        Read-modify-write, so it takes the lock — call it off the event loop
        (``asyncio.to_thread``) since the lock can be held across a Duo reply.
        """
        with self._lock:
            self._budget += max(0, int(n))
            return self._budget

    def budget_remaining(self) -> int:
        """Remaining auto-approvals. Lock-free atomic int read (loop-safe)."""
        return self._budget

    def clear_budget(self) -> int:
        """Zero the budget (window ended); returns what was left. Lock-free.

        Deliberately does NOT take the engine lock: it is called from the event
        loop (``TwoFAService._run_burst`` teardown), and that lock can be held by
        ``handle_transactions`` across a Duo HTTP reply — blocking on it here would
        stall the whole loop. Safe without the lock because :meth:`grant` (the only
        writer that could race a read-then-store) is called solely from
        ``start_burst`` under the service's ``burst_lock``, and the normal teardown
        clears under that SAME ``burst_lock`` — so grant and clear are already
        mutually exclusive. (The abnormal cancellation-path clear runs without
        ``burst_lock``, but only during shutdown, where a lost grant is moot.)
        """
        n = self._budget
        self._budget = 0
        return n

    # ---- transport entrypoint ---------------------------------------------

    def handle_transactions(self, txs: list[Transaction]) -> None:
        with self._lock:
            now = time.monotonic()
            self._expire_holds(now)
            self._prune_acted(now)
            self._drop_resolved_holds({t.urgid for t in txs})

            fresh = [t for t in txs if not self._suppressed(t.urgid, now)]
            if not fresh:
                return

            if self._approve_all_active(now):
                for t in fresh:
                    if self._answer(t, "approve", by="approve-all window"):
                        self.notifier.notify_resolved(t, t.urgid, "approve", "approve-all window")
                return

            # Budget-driven: approve up to the granted budget, oldest-first —
            # already-held txs (longest-waiting) before newly-seen — and hold the
            # overflow. A held tx approved here is a retro-promotion once a later
            # grant frees budget. `_answer` pops it from `_held` on success.
            ordered = sorted(
                fresh,
                key=lambda t: self._held[t.urgid][1] if t.urgid in self._held else now)
            for t in ordered:
                if self._budget > 0:
                    if self._answer(t, "approve", by="auto (budgeted)"):
                        self._budget -= 1
                        self.notifier.notify_single(t)
                elif t.urgid not in self._held:
                    self._held[t.urgid] = (t, now)
                    log.info("holding tx %s (%s) — over budget", t.urgid, t.app)
                    self.notifier.notify_held(t)

    # ---- control surface (driven by the 2fa_* verbs) ----------------------

    def approve(self, urgid: str, by: str = "awm") -> bool:
        with self._lock:
            return self._resolve_held(urgid, "approve", by)

    def deny(self, urgid: str, by: str = "awm") -> bool:
        with self._lock:
            return self._resolve_held(urgid, "deny", by)

    def approve_all(self, by: str = "awm") -> int:
        with self._lock:
            now = time.monotonic()
            self._approve_all_until = now + self.approve_all_seconds
            held = list(self._held.values())
            count = sum(1 for tx, _ in held if self._answer(tx, "approve", by=f"approve-all ({by})"))
            mins = self.approve_all_seconds / 60.0
            self.notifier.notify_text(
                f"approve-all window open for {mins:.0f} min ({by}); "
                f"cleared {count} held login(s)."
            )
            log.info("approve-all opened by %s; cleared %d held", by, count)
            return count

    # ---- read-only views (for the 2fa_status / 2fa_pending verbs) ---------

    def held_transactions(self) -> list[Transaction]:
        """Snapshot of currently-held (burst) transactions, oldest first."""
        with self._lock:
            return [tx for tx, _ in sorted(self._held.values(), key=lambda e: e[1])]

    def approve_all_remaining(self) -> float:
        """Seconds left on the approve-all window (0 if not active)."""
        with self._lock:
            return max(0.0, self._approve_all_until - time.monotonic())

    # ---- internals --------------------------------------------------------

    def _approve_all_active(self, now: float) -> bool:
        return now < self._approve_all_until

    def _suppressed(self, urgid: str, now: float) -> bool:
        ts = self._acted.get(urgid)
        return ts is not None and (now - ts) < self.dedup_seconds

    def _answer(self, tx: Transaction, answer: str, by: str) -> bool:
        """Send the answer to Duo and record it. Returns True on success.

        Pure w.r.t. notifications — callers decide what (if anything) to post.
        """
        try:
            self.client.reply_transaction(tx.urgid, answer)
        except Exception as e:
            log.error("failed to %s tx %s: %s", answer, tx.urgid, e)
            self.notifier.notify_text(f"Failed to {answer} `{tx.urgid}`: {e}")
            return False
        self._acted[tx.urgid] = time.monotonic()
        self._held.pop(tx.urgid, None)
        if answer == "approve":
            self.approved_count += 1
        log.info("%s tx %s (%s) [%s]", answer, tx.urgid, tx.app, by)
        return True

    def _resolve_held(self, urgid: str, answer: str, by: str) -> bool:
        entry = self._held.get(urgid)
        if entry is None:
            # Either already handled, or an unknown id from a stale button.
            if urgid in self._acted:
                log.info("ignoring %s for already-handled %s", answer, urgid)
            else:
                log.warning("no held tx %s to %s", urgid, answer)
            return False
        tx, _ = entry
        ok = self._answer(tx, answer, by=f"manual:{by}")
        if ok:
            self.notifier.notify_resolved(tx, urgid, answer, by)
        return ok

    def _expire_holds(self, now: float) -> None:
        expired = [tx for tx, added in self._held.values()
                   if (now - added) > self.hold_ttl_seconds]
        for tx in expired:
            self._held.pop(tx.urgid, None)
        if expired:
            self.notifier.notify_expired(expired)

    def _prune_acted(self, now: float) -> None:
        ttl = max(self.dedup_seconds, 60.0)
        for urgid in [u for u, ts in self._acted.items() if (now - ts) > ttl]:
            self._acted.pop(urgid, None)

    def _drop_resolved_holds(self, present: set[str]) -> None:
        """Held txs that no longer appear as pending were answered elsewhere."""
        for urgid in [u for u in self._held if u not in present]:
            log.info("held tx %s no longer pending; dropping", urgid)
            self._held.pop(urgid, None)

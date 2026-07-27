"""The loop: sample, judge, act, explain.

The whole licence for this service to exist is that it is invisible, so the
loop is tiered rather than uniform:

* every tick (2 s) it reads four global files — 0.036 ms — and nothing else;
* it takes a **full** process pass (4.9 ms: scan, attribute, roll up) only when
  that global read says the box is doing something, plus a slow heartbeat to
  keep the rollups and CPU deltas honest while idle;
* it reads **accurate** per-process memory (~6 ms for a 28-process session)
  once, for one session, at the moment a decision is about to be taken.

Measured: 0.24% of one core if every tick took a full pass, ~0.02% idle. The
one read that would blow the budget — proportional memory across the whole box,
333 ms — is never scheduled, only ever aimed.

Two safety postures are wired in at this level rather than left to the caller:
**dry-run**, which runs every judgement and records every decision but signals
nothing, and the **arming guard**, which keeps a dev sandbox's copy of this
service inert. Services are auto-bootstrapped by every gateway including each
sandbox, and sandboxes share one process table — without the guard, starting a
sandbox would put a second armed watchdog on the same processes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from awm.compute import notices, store
from awm.compute.action import (
    Candidate,
    Unsafe,
    ancestor_pids,
    assert_safe,
    can_restore_priority,
    deprioritize,
    restore_priority,
    select_victim,
    terminate,
)
from awm.compute.attribution import Attributor
from awm.compute.boxfacts import GIB, Pressure, read_box, read_pressure, uptime_ticks
from awm.compute.policy import Judge, Thresholds, Violation
from awm.compute.probe import CLK_TCK, read_cmdline, scan
from awm.compute.usage import SessionUsage, UsageTracker, accurate_memory, subtree

log = logging.getLogger("awm.compute.watcher")

TICK_S = 2.0
HEARTBEAT_S = 20.0
SNAPSHOT_EVERY_TICKS = 3
#: Gateway origin the watchdog is allowed to arm on. Everything else — every
#: dev sandbox — runs observation-only.
PROD_PORT = int(os.environ.get("AWM_COMPUTE_ARM_PORT", "7819"))


def _origin_port(url: str) -> int | None:
    try:
        return int(url.rsplit(":", 1)[1].split("/")[0])
    except (IndexError, ValueError):
        return None


@dataclass(slots=True)
class Watcher:
    box: Any = None
    judge: Judge | None = None
    attributor: Attributor = field(default_factory=Attributor)
    tracker: UsageTracker = field(default_factory=UsageTracker)

    pressure: Pressure | None = None
    usages: dict[str, SessionUsage] = field(default_factory=dict)
    violations: list[Violation] = field(default_factory=list)

    armed: bool = False
    dry_run: bool = True
    arm_eligible: bool = False
    can_restore: bool = False
    remote_hint: str = ""
    #: Scope the watchdog to a single session id. Empty means every session.
    #: This exists so the synthetic-hog verification can run against
    #: deliberately tiny caps without those caps applying to the eleven real
    #: agent sessions sharing the box — and, in anger, as a way to watch one
    #: suspect session without arming against everything.
    only_session: str = ""

    #: session -> {"pids": [...], "at": monotonic} for lifted-on-recovery renice.
    deprioritized: dict[str, dict] = field(default_factory=dict)

    ticks: int = 0
    full_passes: int = 0
    last_full_pass: float = 0.0
    last_pass_cost_ms: float = 0.0
    started_at: float = 0.0
    #: Share of processes carrying a session id, so a harness rename that
    #: silently breaks attribution is loud rather than invisible.
    attributed_share: float = 0.0
    _low_attribution_warned: bool = False

    # -- setup --------------------------------------------------------------

    def setup(self) -> None:
        store.init()
        self.box = read_box()
        settings = store.get_settings()
        self.judge = Judge(box=self.box, thresholds=Thresholds.load(settings))
        hub = os.environ.get("AWM_HUB_URL", "")
        self.arm_eligible = _origin_port(hub) == PROD_PORT
        self.armed = bool(settings.get("armed", False)) and self.arm_eligible
        self.dry_run = bool(settings.get("dry_run", True))
        self.remote_hint = settings.get("remote_hint") or notices.DEFAULT_REMOTE_HINT
        self.only_session = str(settings.get("only_session") or "")
        self.can_restore = can_restore_priority()
        self.started_at = time.monotonic()
        log.info(
            "compute watchdog: %d cores, %.0f GiB, caps %s; hub=%s arm_eligible=%s "
            "armed=%s dry_run=%s restore_nice=%s",
            self.box.nproc, self.box.mem_total_b / GIB, self.judge.caps.as_dict(),
            hub, self.arm_eligible, self.armed, self.dry_run, self.can_restore,
        )

    def reload_settings(self) -> None:
        settings = store.get_settings()
        assert self.judge is not None
        self.judge.thresholds = Thresholds.load(settings)
        self.armed = bool(settings.get("armed", False)) and self.arm_eligible
        self.dry_run = bool(settings.get("dry_run", True))
        self.remote_hint = settings.get("remote_hint") or notices.DEFAULT_REMOTE_HINT
        self.only_session = str(settings.get("only_session") or "")

    # -- the loop -----------------------------------------------------------

    async def run(self) -> None:
        await asyncio.to_thread(self.setup)
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:  # noqa: BLE001 - a watchdog must not die
                log.exception("compute watchdog tick failed")
            await asyncio.sleep(TICK_S)

    def tick(self) -> None:
        self.ticks += 1
        self.pressure = read_pressure(self.pressure)
        if self.ticks % SNAPSHOT_EVERY_TICKS == 0:
            self._write_snapshot()
        if self._needs_full_pass():
            self.full_pass()

    def _needs_full_pass(self) -> bool:
        """Gate the ~6 ms pass behind the 0.036 ms one.

        The thresholds are "is anything worth looking at", not "is the box in
        trouble" — the full pass has to be running well before a violation so
        the CPU deltas and the dwell clocks are already warm when one appears.
        They are also not zero: a machine hosting a dozen idling agents shows a
        small but permanently non-zero PSI figure, and a gate keyed on ``> 0``
        would open on every tick and be no gate at all.
        """
        now = time.monotonic()
        if now - self.last_full_pass >= HEARTBEAT_S:
            return True
        p, t = self.pressure, self.judge.thresholds  # type: ignore[union-attr]
        if p is None:
            return False
        return (
            p.cpu_busy_cores >= self.box.nproc * 0.15
            or p.mem_available_b < t.mem_avail_floor_gb * 2 * GIB
            or p.psi_mem_some10 >= 1.0
        )

    def full_pass(self) -> None:
        t0 = time.perf_counter()
        procs = scan()
        sid_by_pid = self.attributor.resolve_all(procs)
        up = uptime_ticks()
        self.usages = self.tracker.update(procs, sid_by_pid, up)
        if self.only_session:
            self.usages = {k: v for k, v in self.usages.items()
                           if k == self.only_session}
        attributed = sum(1 for v in sid_by_pid.values() if v)
        self.attributed_share = attributed / max(1, len(procs))
        self._check_attribution_health(attributed)

        assert self.judge is not None and self.pressure is not None
        grants = store.active_grants()
        self.violations = self.judge.evaluate(self.usages, self.pressure, grants)

        self._maybe_restore(procs)

        now = time.monotonic()
        if self.violations and not self.judge.in_quiet_period(now):
            self._handle(self.violations[0], procs, sid_by_pid, up)

        self.last_full_pass = now
        self.full_passes += 1
        self.last_pass_cost_ms = (time.perf_counter() - t0) * 1000.0

    def _check_attribution_health(self, attributed: int) -> None:
        """Attribution rests on a harness-internal environment variable.

        It is stable today and the harness's own tooling uses it, but a future
        Claude Code release could rename it — at which point this service would
        silently police nothing. So the collapse is logged loudly rather than
        left to be noticed months later.
        """
        if attributed == 0 and self.full_passes > 5 and not self._low_attribution_warned:
            self._low_attribution_warned = True
            log.error(
                "compute watchdog: ZERO processes carry %s — attribution is "
                "broken (harness rename?); the watchdog is policing nothing",
                "CLAUDE_CODE_SESSION_ID",
            )
        elif attributed > 0:
            self._low_attribution_warned = False

    # -- decisions ----------------------------------------------------------

    def _handle(
        self,
        v: Violation,
        procs: dict,
        sid_by_pid: dict[int, str | None],
        up: int,
    ) -> None:
        u = self.usages.get(v.session_id)
        if u is None:
            return
        cand, ranked = select_victim(
            v.metric, set(u.pids), u.job_roots, procs,
            self.tracker.last_delta, self.tracker.last_dt, CLK_TCK, up,
        )
        if cand is None:
            self._record(v, "none", "refused: no unprotected job root",
                         detail={"ranked": len(ranked)})
            return

        if v.metric == "memory":
            self._handle_memory(v, u, cand, procs, sid_by_pid, up)
        else:
            self._handle_cpu(v, u, cand, procs, sid_by_pid, up)

    def _handle_memory(self, v, u, cand: Candidate, procs, sid_by_pid, up) -> None:
        # The screening estimate got us here; only the accurate number may act.
        # Re-read it NOW rather than reusing anything from when the timer opened
        # — ten seconds is long enough for the job to have finished.
        members = [m for m in subtree(cand.pid, procs) if m.pid in set(u.pids)]
        pss, swap, total = accurate_memory([m.pid for m in members])
        # Deliberately strict: the CANDIDATE's own subtree must clear the bar,
        # not the session's total. A session can be over its cap because of
        # several modest jobs plus an MCP server we would never kill, and
        # killing whichever unprotected root happened to rank first would cost
        # an agent its work and free almost nothing. When no single job is
        # egregious but the box is genuinely squeezed, the pressure path picks
        # it up instead, against the much lower `pressure_min_session_gb` bar.
        cap = v.cap
        if total <= cap:
            self._record(
                v, "none", "dropped: accurate memory below cap", cand=cand,
                detail={"pss_gb": round(pss / GIB, 2),
                        "swap_gb": round(swap / GIB, 2),
                        "rss_estimate_gb": round(u.rss_estimate_b / GIB, 2)},
            )
            return

        if not self.armed or self.dry_run:
            self._record(v, "terminate", self._suppressed_reason(), cand=cand,
                         detail={"pss_gb": round(pss / GIB, 2),
                                 "swap_gb": round(swap / GIB, 2)})
            return

        try:
            group = assert_safe(cand, v.session_id, sid_by_pid, procs,
                                self_pids=ancestor_pids())
        except Unsafe as exc:
            self._record(v, "terminate", f"refused: {exc}", cand=cand)
            return

        result = terminate(cand.pid)
        assert self.judge is not None
        self.judge.note_action(time.monotonic())
        self._record(v, "terminate", "terminated", cand=cand,
                     detail={"pss_gb": round(pss / GIB, 2),
                             "swap_gb": round(swap / GIB, 2),
                             "group_size": len(group), "kill": result})
        self._notify(v, cand, action="terminated",
                     measured_gb=round(total / GIB, 2),
                     cap_gb=round(cap / GIB, 2),
                     swap_gb=round(swap / GIB, 2))

    def _handle_cpu(self, v, u, cand: Candidate, procs, sid_by_pid, up) -> None:
        if v.session_id in self.deprioritized:
            return  # already yielding; nothing further is warranted
        if not self.armed or self.dry_run:
            self._record(v, "deprioritize", self._suppressed_reason(), cand=cand)
            return
        try:
            group = assert_safe(cand, v.session_id, sid_by_pid, procs,
                                self_pids=ancestor_pids())
        except Unsafe as exc:
            self._record(v, "deprioritize", f"refused: {exc}", cand=cand)
            return

        result = deprioritize(group)
        self.deprioritized[v.session_id] = {
            "pids": result["reniced"], "at": time.monotonic(), "root": cand.pid,
        }
        # No quiet period for CPU: nothing was destroyed, so there is no
        # reclaim to wait for, and a second session over the cap deserves the
        # same treatment on the next pass.
        self._record(v, "deprioritize", "deprioritized", cand=cand,
                     detail={**result, "restorable": self.can_restore})
        self._notify(v, cand, action="deprioritized",
                     measured_cores=round(v.measured, 1),
                     cap_cores=round(v.cap, 1), nice=result["nice"])

    def _maybe_restore(self, procs: dict) -> None:
        """Lift a deprioritisation once the session is back under its cap.

        Left in place for the job's lifetime it would be a hidden tax on work
        that is no longer harming anyone. Where ``sudo`` is unavailable this
        cannot succeed — ``RLIMIT_NICE`` is ``(0, 0)`` — and the failure is
        recorded rather than retried forever.
        """
        if not self.deprioritized:
            return
        assert self.judge is not None
        caps, h = self.judge.caps, self.judge.thresholds.hysteresis
        for sid in list(self.deprioritized):
            u = self.usages.get(sid)
            gone = u is None
            recovered = (
                not self.judge.gate.cpu_open
                or (u is not None and u.cpu_cores < caps.soft_cpu_cores * h)
            )
            if not (gone or recovered):
                continue
            entry = self.deprioritized.pop(sid)
            live = [p for p in entry["pids"] if p in procs]
            if not live or not self.can_restore:
                continue
            result = restore_priority(live)
            store.record_decision(
                session_id=sid, metric="cpu", kind="restore",
                measured=(u.cpu_cores if u else 0.0),
                cap=caps.soft_cpu_cores, action="restore",
                outcome="restored" if not result["failed"] else "partial",
                target_pid=entry.get("root"), detail=result,
            )

    def _suppressed_reason(self) -> str:
        if not self.arm_eligible:
            return "suppressed: not the production gateway (observation only)"
        if not self.armed:
            return "suppressed: watchdog disarmed"
        return "suppressed: dry-run"

    def _record(
        self, v: Violation, action: str, outcome: str,
        cand: Candidate | None = None, detail: dict | None = None,
    ) -> None:
        log.info("compute decision: %s %s/%s -> %s (%s)",
                 v.session_id[:8], v.metric, v.kind, action, outcome)
        store.record_decision(
            session_id=v.session_id, metric=v.metric, kind=v.kind,
            measured=v.measured, cap=v.cap, action=action, outcome=outcome,
            target_pid=cand.pid if cand else None,
            cmdline=cand.cmdline if cand else None,
            detail={
                **(detail or {}),
                "box": self.pressure.as_dict() if self.pressure else {},
                "dwell_s": round(v.age(time.monotonic()), 1),
                "candidate": (
                    {"n_procs": cand.n_procs, "age_s": round(cand.age_s),
                     "pgid": cand.pgid} if cand else None
                ),
            },
        )

    def _notify(self, v: Violation, cand: Candidate, *, action: str, **extra) -> None:
        assert self.box is not None
        notices.write_notice(v.session_id, {
            "action": action,
            "metric": v.metric,
            "kind": v.kind,
            "cmdline": cand.cmdline,
            "target_pid": cand.pid,
            "ran_for_s": cand.age_s,
            "box_mem_gb": round(self.box.mem_total_b / GIB, 1),
            "box_cores": self.box.nproc,
            "remote_hint": self.remote_hint,
            "wall_ts": time.time(),
            **extra,
        })

    # -- the pre-launch snapshot -------------------------------------------

    def _write_snapshot(self) -> None:
        p, box = self.pressure, self.box
        if p is None or box is None:
            return
        assert self.judge is not None
        t = self.judge.thresholds
        loaded = (
            p.cpu_busy_cores >= box.nproc * 0.6
            or p.mem_available_b < t.mem_avail_floor_gb * 2 * GIB
            or self.judge.gate.mem_open
            or self.judge.gate.cpu_open
        )
        notices.write_box_snapshot({
            "wall_ts": time.time(),
            "loaded": loaded,
            "box_cores": box.nproc,
            "box_mem_gb": round(box.mem_total_b / GIB, 1),
            "mem_available_gb": round(p.mem_available_b / GIB, 1),
            "cpu_busy_cores": round(p.cpu_busy_cores, 1),
            "swap_pressure": p.swap_free_b < t.swap_free_floor_gb * GIB,
            "armed": self.armed and not self.dry_run,
        })

    # -- read-only surface --------------------------------------------------

    def status(self) -> dict:
        assert self.judge is not None and self.box is not None
        uptime = max(1e-9, time.monotonic() - self.started_at)
        return {
            "box": {"cores": self.box.nproc,
                    "mem_gb": round(self.box.mem_total_b / GIB, 1),
                    "swap_gb": round(self.box.swap_total_b / GIB, 1)},
            "caps": self.judge.caps.as_dict(),
            "thresholds": self.judge.thresholds.as_dict(),
            "pressure": self.pressure.as_dict() if self.pressure else None,
            "gate": self.judge.gate.as_dict(),
            "armed": self.armed,
            "dry_run": self.dry_run,
            "arm_eligible": self.arm_eligible,
            "hub_url": os.environ.get("AWM_HUB_URL", ""),
            "can_restore_priority": self.can_restore,
            "sessions": len(self.usages),
            "attributed_share": round(self.attributed_share, 3),
            "open_timers": self.judge.timers(),
            "deprioritized": {k: v["pids"] for k, v in self.deprioritized.items()},
            "duty": {
                "ticks": self.ticks,
                "full_passes": self.full_passes,
                "last_pass_ms": round(self.last_pass_cost_ms, 2),
                # The user's explicit budget was "under 1% of one core"; this is
                # the number that has to stay honest.
                "duty_cycle_pct_of_one_core": round(
                    self.full_passes * self.last_pass_cost_ms / 1000.0
                    / uptime * 100.0, 4),
                "attribution": self.attributor.stats(),
            },
        }

    def dump(self) -> dict:
        assert self.judge is not None
        return {
            "sessions": sorted(
                (u.as_dict() for u in self.usages.values()),
                key=lambda d: d["rss_estimate_gb"], reverse=True,
            ),
            "violations": [v.as_dict() for v in self.violations],
            "caps": self.judge.caps.as_dict(),
            "note": ("rss_estimate_gb is a SCREENING estimate — it double-counts "
                     "shared pages and has been measured 40%-1200% high. Only "
                     "the accurate Pss+Swap read taken at decision time may "
                     "trigger an action."),
        }

    def explain(self, pid: int) -> dict:
        procs = scan()
        sid_by_pid = self.attributor.resolve_all(procs)
        p = procs.get(pid)
        if p is None:
            return {"pid": pid, "found": False}
        sid = sid_by_pid.get(pid)
        from awm.compute.action import protected_reason
        cmd = read_cmdline(pid)
        chain, cur = [], pid
        for _ in range(24):
            q = procs.get(cur)
            if q is None or cur <= 1:
                break
            chain.append({"pid": cur, "comm": q.comm, "session": sid_by_pid.get(cur)})
            cur = q.ppid
        u = self.usages.get(sid) if sid else None
        return {
            "pid": pid,
            "found": True,
            "cmdline": cmd,
            "session_id": sid,
            "attributed": sid is not None,
            "protected": protected_reason(cmd),
            "is_job_root": bool(u and pid in u.job_roots),
            "subtree_procs": len(subtree(pid, procs)),
            "ancestors": chain,
            "session_usage": u.as_dict() if u else None,
        }

"""When is a session a problem? Two limits and one gate.

**The hard ceiling** always applies. ``nproc - 2`` cores — two left for the
system and the user's own shell — and, as its memory analogue, total memory
minus a fixed reserve. This is the "you are now the whole machine" line and it
does not care what else is happening.

**The soft cap** is half the box, and it is live *only while the box is under
pressure*. On a quiet machine one agent may run well past it, up to the hard
ceiling: nothing is being harmed, and stopping a four-hour build because it
used nine cores on an idle box is pure loss. This is the freedom half of the
brief, and it is deliberate, not an oversight.

**The pressure gate** decides when the soft cap is live, and also catches the
case no per-session cap can. Five sessions at 30% each is the realistic route
to a wedged machine and not one of them is over any cap; when available memory
falls under the floor, the largest contributor is acted on regardless. Both PSI
and the absolute floor must agree before this fires — PSI alone spikes during
ordinary heavy work while 58 GiB is still free.

Two asymmetries are load-bearing:

* **CPU never kills.** With 16 cores a saturated box is sluggish and
  self-correcting; memory exhaustion driving swap thrash is what actually
  freezes it. So CPU gets deprioritised and memory gets terminated.
* **The dwell timer starts when the gate opens, not when the session crossed
  the line.** A session sitting happily at 60% of the box becomes a violation
  the instant another agent starts and pressure rises. Timing it from the
  crossing would fire immediately, with no grace at all.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from awm.compute.boxfacts import GIB, Box, Pressure
from awm.compute.usage import SessionUsage


@dataclass(slots=True)
class Thresholds:
    """Everything tunable, derived from box size rather than written as
    literals — the same defaults behave on a 4-core laptop and a 64-core node."""

    #: Cores left unclaimable at the hard ceiling.
    cpu_headroom_cores: int = 2
    #: Memory left unclaimable at the hard ceiling.
    mem_reserve_gb: float = 8.0
    #: Soft cap as a fraction of the box. "Half" per the brief.
    soft_fraction: float = 0.5

    #: Pressure gate — BOTH the PSI signal and the absolute floor must agree.
    mem_avail_floor_gb: float = 8.0
    psi_mem_some10: float = 5.0
    swap_free_floor_gb: float = 4.0
    psi_cpu_some10: float = 40.0
    #: Fraction of cores busy before CPU counts as pressured.
    cpu_busy_fraction: float = 0.85

    #: A session below this is never the pressure-gate victim, however large a
    #: share of the remainder it holds.
    pressure_min_session_gb: float = 4.0

    #: How long a violation must persist before it is acted on.
    dwell_mem_s: float = 10.0
    dwell_cpu_s: float = 30.0
    #: Clear a timer only once the session drops this far below its cap, so a
    #: job oscillating on the line does not reset its clock every sample.
    hysteresis: float = 0.9
    #: After any action, hold off entirely while memory reclaim catches up.
    quiet_period_s: float = 60.0

    @classmethod
    def load(cls, overrides: dict[str, Any] | None = None) -> "Thresholds":
        t = cls()
        for k, v in (overrides or {}).items():
            if hasattr(t, k) and isinstance(v, (int, float)):
                setattr(t, k, type(getattr(t, k))(v))
        return t

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Caps:
    hard_cpu_cores: float
    soft_cpu_cores: float
    hard_mem_b: int
    soft_mem_b: int

    def as_dict(self) -> dict:
        return {
            "hard_cpu_cores": self.hard_cpu_cores,
            "soft_cpu_cores": self.soft_cpu_cores,
            "hard_mem_gb": round(self.hard_mem_b / GIB, 1),
            "soft_mem_gb": round(self.soft_mem_b / GIB, 1),
        }


def derive_caps(box: Box, t: Thresholds) -> Caps:
    hard_cpu = max(1.0, float(box.nproc - t.cpu_headroom_cores))
    soft_cpu = max(1.0, box.nproc * t.soft_fraction)
    hard_mem = max(GIB, int(box.mem_total_b - t.mem_reserve_gb * GIB))
    soft_mem = max(GIB, int(box.mem_total_b * t.soft_fraction))
    return Caps(
        hard_cpu_cores=hard_cpu,
        soft_cpu_cores=min(soft_cpu, hard_cpu),
        hard_mem_b=hard_mem,
        soft_mem_b=min(soft_mem, hard_mem),
    )


@dataclass(slots=True)
class Gate:
    """Whether the soft caps are currently live, and since when."""

    mem_open: bool = False
    cpu_open: bool = False
    mem_opened_at: float = 0.0
    cpu_opened_at: float = 0.0

    def update(
        self, p: Pressure, box: Box, t: Thresholds, now: float
    ) -> set[str]:
        """Recompute both gates; return the metrics that *just* opened."""
        mem = (
            p.mem_available_b < t.mem_avail_floor_gb * GIB
            and (p.psi_mem_some10 >= t.psi_mem_some10
                 or p.swap_free_b < t.swap_free_floor_gb * GIB)
        )
        cpu = (
            p.cpu_busy_cores >= box.nproc * t.cpu_busy_fraction
            or p.psi_cpu_some10 >= t.psi_cpu_some10
        )
        opened: set[str] = set()
        if mem and not self.mem_open:
            self.mem_opened_at = now
            opened.add("memory")
        if cpu and not self.cpu_open:
            self.cpu_opened_at = now
            opened.add("cpu")
        self.mem_open, self.cpu_open = mem, cpu
        return opened

    def as_dict(self) -> dict:
        return {"mem_open": self.mem_open, "cpu_open": self.cpu_open}


@dataclass(slots=True)
class Violation:
    session_id: str
    metric: str          # "memory" | "cpu"
    kind: str            # "hard" | "soft" | "pressure"
    measured: float      # cores, or bytes
    cap: float           # cores, or bytes
    since: float         # monotonic; when the dwell clock started
    def age(self, now: float) -> float:
        return now - self.since

    def as_dict(self) -> dict:
        d = {
            "session_id": self.session_id,
            "metric": self.metric,
            "kind": self.kind,
            "cap": self.cap,
            "measured": self.measured,
        }
        if self.metric == "memory":
            d["measured_gb"] = round(self.measured / GIB, 2)
            d["cap_gb"] = round(self.cap / GIB, 2)
        return d


@dataclass(slots=True)
class Judge:
    """Stateful: it owns the dwell timers, which is all the memory it needs."""

    box: Box
    thresholds: Thresholds
    gate: Gate = field(default_factory=Gate)
    #: (session_id, metric) -> (dwell clock start, violation kind).
    _timers: dict[tuple[str, str], tuple[float, str]] = field(default_factory=dict)
    last_action_at: float = 0.0

    @property
    def caps(self) -> Caps:
        return derive_caps(self.box, self.thresholds)

    def in_quiet_period(self, now: float) -> bool:
        return bool(self.last_action_at) and (
            now - self.last_action_at < self.thresholds.quiet_period_s
        )

    def note_action(self, now: float) -> None:
        """After acting, clear every timer rather than pausing them.

        Pausing would let a second action land on an innocent sibling the
        moment the quiet period lapses, before reclaim has had any effect.
        """
        self.last_action_at = now
        self._timers.clear()

    def evaluate(
        self,
        usages: dict[str, SessionUsage],
        pressure: Pressure,
        grants: dict[str, dict],
        now: float | None = None,
    ) -> list[Violation]:
        now = time.monotonic() if now is None else now
        t, caps = self.thresholds, self.caps
        opened = self.gate.update(pressure, self.box, t, now)
        # A gate reopening restarts the grace period for everything it governs.
        # Without this, a session that has been comfortably over the soft cap
        # through several quiet spells carries a stale, long-expired clock and
        # is acted on the instant pressure returns. Hard violations keep their
        # clocks — the gate has no bearing on them.
        for (sid, metric), (_, kind) in list(self._timers.items()):
            if metric in opened and kind != "hard":
                del self._timers[(sid, metric)]

        raw: list[Violation] = []
        for sid, u in usages.items():
            grant = grants.get(sid)
            cpu_cap_hard = caps.hard_cpu_cores
            cpu_cap_soft = caps.soft_cpu_cores
            mem_cap_hard = float(caps.hard_mem_b)
            mem_cap_soft = float(caps.soft_mem_b)
            if grant:
                # A grant raises this session's ceilings; it never lowers them.
                if grant.get("cpu_cores"):
                    cpu_cap_soft = max(cpu_cap_soft, float(grant["cpu_cores"]))
                    cpu_cap_hard = max(cpu_cap_hard, float(grant["cpu_cores"]))
                if grant.get("mem_gb"):
                    g = float(grant["mem_gb"]) * GIB
                    mem_cap_soft = max(mem_cap_soft, g)
                    mem_cap_hard = max(mem_cap_hard, g)

            mem = float(u.rss_estimate_b)
            if mem > mem_cap_hard:
                raw.append(Violation(sid, "memory", "hard", mem, mem_cap_hard, 0.0))
            elif self.gate.mem_open and mem > mem_cap_soft:
                raw.append(Violation(sid, "memory", "soft", mem, mem_cap_soft,
                                     self.gate.mem_opened_at))

            cpu = u.cpu_cores
            if cpu > cpu_cap_hard:
                raw.append(Violation(sid, "cpu", "hard", cpu, cpu_cap_hard, 0.0))
            elif self.gate.cpu_open and cpu > cpu_cap_soft:
                raw.append(Violation(sid, "cpu", "soft", cpu, cpu_cap_soft,
                                     self.gate.cpu_opened_at))

        raw.extend(self._pressure_violations(usages, grants, raw))
        return self._apply_timers(raw, usages, now)

    def _pressure_violations(
        self,
        usages: dict[str, SessionUsage],
        grants: dict[str, dict],
        already: list[Violation],
    ) -> list[Violation]:
        """The many-sessions case: nobody over a cap, box squeezed anyway.

        Only ever nominates the single largest contributor, and only if it is
        big enough to be worth blaming — otherwise a box squeezed by something
        that is not an agent at all would take an agent's job with it.
        """
        if not self.gate.mem_open:
            return []
        t = self.thresholds
        floor = t.pressure_min_session_gb * GIB
        # A session already over a real cap is being handled on its own terms;
        # adding a second, weaker violation for it would just duplicate the
        # record and the notice.
        flagged = {v.session_id for v in already if v.metric == "memory"}
        eligible = [
            u for sid, u in usages.items()
            if u.rss_estimate_b >= floor and sid not in grants and sid not in flagged
        ]
        if not eligible:
            return []
        biggest = max(eligible, key=lambda u: u.rss_estimate_b)
        return [Violation(
            biggest.session_id, "memory", "pressure",
            float(biggest.rss_estimate_b), floor, self.gate.mem_opened_at,
        )]

    def _apply_timers(
        self,
        raw: list[Violation],
        usages: dict[str, SessionUsage],
        now: float,
    ) -> list[Violation]:
        """Attach dwell clocks; drop entries that have not persisted yet.

        Every clock starts at first observation, and — because a soft or
        pressure violation cannot be observed before its gate opens, and the
        caller clears those clocks when the gate reopens — that is the same
        thing as starting it when the gate opened. A session that has been
        sitting at 60% of the box for an hour therefore gets its full grace
        period the moment pressure makes 60% a problem, rather than being
        judged retroactively for the hour it spent doing nothing wrong.
        """
        seen: set[tuple[str, str]] = set()
        ripe: list[Violation] = []
        for v in raw:
            key = (v.session_id, v.metric)
            seen.add(key)
            entry = self._timers.get(key)
            if entry is None:
                entry = (now, v.kind)
                self._timers[key] = entry
            v.since = entry[0]
            dwell = (self.thresholds.dwell_mem_s if v.metric == "memory"
                     else self.thresholds.dwell_cpu_s)
            if v.age(now) >= dwell:
                ripe.append(v)

        # Hysteresis: only forget a timer once the session is comfortably back
        # under, so a job bouncing on the line does not reset its clock forever.
        for key in list(self._timers):
            if key in seen:
                continue
            sid, metric = key
            u = usages.get(sid)
            if u is None:
                del self._timers[key]
                continue
            caps = self.caps
            h = self.thresholds.hysteresis
            under = (
                u.rss_estimate_b < caps.soft_mem_b * h if metric == "memory"
                else u.cpu_cores < caps.soft_cpu_cores * h
            )
            if under:
                del self._timers[key]

        ripe.sort(key=lambda v: (v.metric != "memory", v.kind != "hard"))
        return ripe

    def timers(self) -> dict[str, float]:
        now = time.monotonic()
        return {
            f"{s}:{m}": round(now - t0, 1)
            for (s, m), (t0, _) in self._timers.items()
        }

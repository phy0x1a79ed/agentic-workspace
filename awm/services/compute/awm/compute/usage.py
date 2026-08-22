"""Rolling per-session usage: cores in use, memory estimate, job roots.

The CPU accounting here is the part worth reading carefully, because the naive
version is silently blind to exactly the workload this service exists for.

A ``make -j16`` of five hundred sub-second compiles has almost no *own* CPU at
any instant a 2-second sampler happens to look — every child is born and reaped
between samples. Their time survives only in the parent's ``cutime``/``cstime``,
which accumulate at ``wait()``. So a session's CPU is the delta of
``utime + stime + cutime + cstime`` summed over its live processes.

That sum needs one correction. When a process dies, its total is folded into
whichever live ancestor reaped it, so that ancestor's delta jumps by the dead
process's *entire lifetime* — including the part already counted in earlier
intervals. :meth:`UsageTracker.update` subtracts each departed process's last
known total, which leaves precisely the work it did inside this interval.

Memory here is the resident-set sum and is labelled ``rss_estimate_b``
everywhere it surfaces, because it is one: it double-counts shared pages, and
measured against the truth on live sessions it overshot by +40% to +1200%. It
is fit to *rank* sessions and to decide who is worth a closer look. The number
a decision is taken on comes from :func:`awm.compute.probe.read_pss_swap`, at
decision time, for one session only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from awm.compute.probe import CLK_TCK, Proc, ProcKey, read_pss_swap

GIB = 1024 ** 3


def subtree(root_pid: int, procs: dict[int, Proc]) -> list[Proc]:
    """Every live descendant of ``root_pid``, inclusive."""
    children: dict[int, list[int]] = {}
    for p in procs.values():
        children.setdefault(p.ppid, []).append(p.pid)
    out, stack, seen = [], [root_pid], {root_pid}
    while stack:
        pid = stack.pop()
        p = procs.get(pid)
        if p is not None:
            out.append(p)
        for child in children.get(pid, ()):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return out


def accurate_memory(pids: list[int]) -> tuple[int, int, int]:
    """``(pss, swap, total)`` bytes for a set of pids — the decision number.

    Proportional set size divides shared pages among their sharers, so this is
    what the tree actually costs the box, and swap is folded in because swap
    occupancy is what wedges this machine. ~1.1 ms per process: fine for one
    session at decision time, ruinous on a schedule.
    """
    pss = swap = 0
    for pid in pids:
        got = read_pss_swap(pid)
        if got is None:
            continue  # exited mid-read; its memory is already back
        pss += got[0]
        swap += got[1]
    return pss, swap, pss + swap


@dataclass(slots=True)
class SessionUsage:
    """One agent session's footprint as of the latest full pass."""

    session_id: str
    pids: list[int]
    job_roots: list[int]
    cpu_cores: float
    rss_estimate_b: int
    n_procs: int
    oldest_start_ticks: int
    newest_start_ticks: int

    def as_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "n_procs": self.n_procs,
            "cpu_cores": round(self.cpu_cores, 2),
            "rss_estimate_gb": round(self.rss_estimate_b / GIB, 2),
            "job_roots": self.job_roots,
        }


@dataclass(slots=True)
class UsageTracker:
    """Carries the previous sample so CPU can be a rate rather than a total."""

    _prev_total: dict[ProcKey, int] = field(default_factory=dict)
    _prev_session: dict[ProcKey, str | None] = field(default_factory=dict)
    #: ``None`` until the first sample. A sentinel, not ``0.0`` — a monotonic
    #: clock that legitimately reads zero would otherwise look like "no
    #: previous sample" forever and the CPU rate would stay pinned at zero.
    _prev_ts: float | None = None
    _prev_uptime_ticks: int = 0
    #: Per-process CPU ticks attributed to the last interval, and that
    #: interval's length. Kept so victim selection can rank a session's job
    #: roots by the CPU their own subtrees are burning, rather than by a
    #: lifetime total that would always name the oldest process.
    last_delta: dict[ProcKey, int] = field(default_factory=dict)
    last_dt: float = 0.0

    def update(
        self,
        procs: dict[int, Proc],
        sid_by_pid: dict[int, str | None],
        uptime_ticks: int,
        now: float | None = None,
    ) -> dict[str, SessionUsage]:
        now = time.monotonic() if now is None else now
        dt = now - self._prev_ts if self._prev_ts is not None else 0.0
        interval_ticks = uptime_ticks - self._prev_uptime_ticks

        by_session: dict[str, list[Proc]] = {}
        for pid, proc in procs.items():
            sid = sid_by_pid.get(pid)
            if sid:
                by_session.setdefault(sid, []).append(proc)

        # Departed processes: their last recorded total has been folded into a
        # surviving ancestor's `cutime`, so charge it back out.
        live_keys = {p.key for p in procs.values()}
        departed: dict[str, int] = {}
        for key, total in self._prev_total.items():
            if key in live_keys:
                continue
            sid = self._prev_session.get(key)
            if sid:
                departed[sid] = departed.get(sid, 0) + total

        self.last_delta = {}
        self.last_dt = dt
        usages: dict[str, SessionUsage] = {}
        for sid, members in by_session.items():
            pid_set = {p.pid for p in members}
            delta_ticks = 0
            for p in members:
                prev = self._prev_total.get(p.key)
                if prev is not None:
                    d = p.total_ticks - prev
                elif interval_ticks > 0 and (uptime_ticks - p.start_ticks) <= interval_ticks:
                    # Born inside this interval — all of its CPU belongs here.
                    d = p.total_ticks
                else:
                    # Pre-existing but first seen now (startup, or newly
                    # attributed). Counting its lifetime total would report a
                    # months-old process as pegging the box, so it starts at
                    # zero and is measured from the next pass.
                    d = 0
                self.last_delta[p.key] = d
                delta_ticks += d
            delta_ticks -= departed.get(sid, 0)

            cores = 0.0
            if dt > 0 and delta_ticks > 0:
                cores = delta_ticks / CLK_TCK / dt

            usages[sid] = SessionUsage(
                session_id=sid,
                pids=sorted(pid_set),
                job_roots=sorted(
                    p.pid for p in members
                    if p.ppid not in pid_set
                ),
                cpu_cores=cores,
                rss_estimate_b=sum(p.rss_bytes for p in members),
                n_procs=len(members),
                oldest_start_ticks=min(p.start_ticks for p in members),
                newest_start_ticks=max(p.start_ticks for p in members),
            )

        self._prev_total = {p.key: p.total_ticks for p in procs.values()}
        self._prev_session = {
            p.key: sid_by_pid.get(p.pid) for p in procs.values()
        }
        self._prev_ts = now
        self._prev_uptime_ticks = uptime_ticks
        return usages

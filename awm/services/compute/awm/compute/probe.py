"""Raw ``/proc`` reads — the only place in this service that parses procfs.

Everything here is deliberately cheap and deliberately tiered, because the
watchdog's whole licence to exist is that it costs nothing. Measured on this
box (386 pids):

===========================================  ==========  ====================
read                                          cpu cost    who calls it
===========================================  ==========  ====================
:func:`read_global_files` (4 files)             0.036 ms  every tick
:func:`scan` (all ``stat``, parsed)              3.2 ms   full pass only
:func:`read_cmdline` (all pids)                  2.1 ms   cached, on demand
:func:`read_environ` (all pids)                  2.2 ms   unattributed roots
:func:`read_pss_swap` (all pids)                  333 ms  **never** for all
===========================================  ==========  ====================

That last row is why :func:`read_pss_swap` takes a single pid and is called
only for the one session a decision is about to be taken on (~6 ms for a
28-process session). Summing it across the box on a schedule would cost ~17%
of a core and is the mistake this module's shape exists to prevent.

No psutil: the gateway tree already parses ``/proc`` by hand
(``awm.gateway._process_utils``), and a watchdog that must never be the cause
of an outage should not carry a third-party dependency it can survive without.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

CLK_TCK: int = os.sysconf("SC_CLK_TCK")
PAGE_SIZE: int = os.sysconf("SC_PAGE_SIZE")

#: Key identifying a process across samples. The start time is load-bearing:
#: pids are recycled, and a cache keyed on pid alone eventually attributes a
#: fresh process to whatever session used to own that number.
ProcKey = tuple[int, int]


@dataclass(slots=True)
class Proc:
    """One process, as of one sample. Fields are raw ``/proc/<pid>/stat``."""

    pid: int
    ppid: int
    pgid: int
    comm: str
    state: str
    utime: int          # ticks, this process, user
    stime: int          # ticks, this process, system
    cutime: int         # ticks, reaped descendants, user
    cstime: int         # ticks, reaped descendants, system
    num_threads: int
    start_ticks: int    # since boot
    rss_pages: int
    nice: int

    @property
    def key(self) -> ProcKey:
        return (self.pid, self.start_ticks)

    @property
    def own_ticks(self) -> int:
        return self.utime + self.stime

    @property
    def reaped_ticks(self) -> int:
        """CPU of descendants this process has already waited on.

        The reason the watchdog can see a parallel build at all: a `make -j16`
        of hundreds of sub-second compiles has almost no *own* CPU, and every
        child is gone before the next sample. Their time survives only here.
        """
        return self.cutime + self.cstime

    @property
    def total_ticks(self) -> int:
        return self.own_ticks + self.reaped_ticks

    @property
    def rss_bytes(self) -> int:
        """Resident size — a **screening estimate only**, never a trigger.

        Summed across a process tree this double-counts every shared page.
        Measured against the truth on live sessions here, the sum overshot by
        +40% on a single-process session and +1200% on a shell-heavy one. See
        :func:`read_pss_swap` for the number a decision may be taken on.
        """
        return self.rss_pages * PAGE_SIZE


def iter_pids() -> list[int]:
    out: list[int] = []
    for name in os.listdir("/proc"):
        if name[0].isdigit():
            try:
                out.append(int(name))
            except ValueError:
                pass
    return out


def read_stat(pid: int) -> Proc | None:
    """Parse ``/proc/<pid>/stat``. ``None`` if the process is already gone."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    # comm is parenthesised and may itself contain spaces and ')' — split on
    # the LAST ')', which is the only unambiguous boundary in this format.
    close = raw.rfind(b")")
    if close == -1:
        return None
    open_paren = raw.find(b"(")
    if open_paren == -1 or open_paren > close:
        return None
    comm = raw[open_paren + 1:close].decode("utf-8", "replace")
    rest = raw[close + 2:].split()
    # rest[0] is field 3 (state); field N lives at rest[N - 3].
    if len(rest) < 22:
        return None
    try:
        return Proc(
            pid=pid,
            ppid=int(rest[1]),
            pgid=int(rest[2]),
            comm=comm,
            state=rest[0].decode(),
            utime=int(rest[11]),
            stime=int(rest[12]),
            cutime=int(rest[13]),
            cstime=int(rest[14]),
            nice=int(rest[16]),
            num_threads=int(rest[17]),
            start_ticks=int(rest[19]),
            rss_pages=int(rest[21]),
        )
    except (ValueError, IndexError):
        return None


def scan(pids: list[int] | None = None) -> dict[int, Proc]:
    """Sample every process on the box. ~3.2 ms for ~390 pids."""
    procs: dict[int, Proc] = {}
    for pid in (pids if pids is not None else iter_pids()):
        p = read_stat(pid)
        if p is not None:
            procs[pid] = p
    return procs


def read_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def read_environ(pid: int) -> dict[str, str]:
    """Read a process's environment. ~6 us; the whole box is ~2.2 ms.

    Fails silently and completely on a process we cannot read (a root-owned
    one, or one that exited mid-read) — the caller treats an empty environment
    as "not an agent's", which is the safe direction: an unattributed process
    is never acted on.
    """
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            raw = fh.read()
    except OSError:
        return {}
    env: dict[str, str] = {}
    for chunk in raw.split(b"\x00"):
        if not chunk:
            continue
        k, sep, v = chunk.partition(b"=")
        if sep:
            env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
    return env


def read_pss_swap(pid: int) -> tuple[int, int] | None:
    """Proportional set size and swap for one pid, in bytes.

    Proportional means shared pages are divided among their sharers, so summing
    this across a process tree gives the memory the tree actually costs the
    box. This is the only memory number allowed to trigger an action, and swap
    is included because swap occupancy is the thing that actually wedges this
    machine.

    ~1.1 ms per process. Call it for one session at decision time, never in a
    sampling loop.
    """
    try:
        with open(f"/proc/{pid}/smaps_rollup", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    pss_kb = swap_kb = 0
    for line in raw.splitlines():
        if line.startswith(b"Pss:"):
            pss_kb = int(line.split()[1])
        elif line.startswith(b"Swap:"):
            swap_kb = int(line.split()[1])
    return pss_kb * 1024, swap_kb * 1024


def pid_alive(pid: int, start_ticks: int | None = None) -> bool:
    """Liveness, optionally pinned to a start time so pid reuse reads as dead."""
    p = read_stat(pid)
    if p is None:
        return False
    if start_ticks is not None and p.start_ticks != start_ticks:
        return False
    return p.state != "Z"

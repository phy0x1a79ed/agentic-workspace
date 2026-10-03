"""How big this box is, and how hard it is currently breathing.

Two halves:

* **Size** (:class:`Box`) — read once at startup. Every threshold in
  :mod:`awm.compute.policy` is derived from these numbers rather than written
  as a literal, so the same service behaves sanely on a 4-core laptop and on a
  64-core node without an edit.
* **Pressure** (:class:`Pressure`) — four files, 0.036 ms, read on every tick.
  This read is the gate: a full process pass only happens when this says the
  box is doing something, which is what keeps the idle duty cycle near 0.02%
  of one core.

Pressure is deliberately two-signal. PSI alone misfires — a first reading of
this box showed memory pressure spiking to 28% during unrelated heavy work
while 58 GiB was still available — so the policy requires PSI *and* the
available-memory floor to agree before it treats the box as squeezed.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from awm.compute.probe import CLK_TCK

GIB = 1024 ** 3


@dataclass(frozen=True, slots=True)
class Box:
    """Static facts about the machine. Read once; nothing here changes."""

    nproc: int
    mem_total_b: int
    swap_total_b: int
    boot_ticks: int  # CLK_TCK ticks of uptime at the moment we read it


def read_box() -> Box:
    mem_total = swap_total = 0
    with open("/proc/meminfo", "rb") as fh:
        for line in fh:
            if line.startswith(b"MemTotal:"):
                mem_total = int(line.split()[1]) * 1024
            elif line.startswith(b"SwapTotal:"):
                swap_total = int(line.split()[1]) * 1024
    with open("/proc/uptime", "rb") as fh:
        up = float(fh.read().split()[0])
    return Box(
        nproc=os.cpu_count() or 1,
        mem_total_b=mem_total,
        swap_total_b=swap_total,
        boot_ticks=int(up * CLK_TCK),
    )


@dataclass(slots=True)
class Pressure:
    """One tick's worth of global state. Cheap enough to take every 2 s."""

    ts: float
    mem_available_b: int
    mem_free_b: int
    swap_free_b: int
    psi_mem_some10: float
    psi_mem_full10: float
    psi_cpu_some10: float
    cpu_busy_cores: float  # cores' worth of non-idle time since the last tick
    #: Opaque carry-over for the next call's CPU delta.
    _stat: tuple[int, int] = field(default=(0, 0), repr=False)

    def as_dict(self) -> dict:
        return {
            "ts": self.ts,
            "mem_available_gb": round(self.mem_available_b / GIB, 2),
            "mem_free_gb": round(self.mem_free_b / GIB, 2),
            "swap_free_gb": round(self.swap_free_b / GIB, 2),
            "psi_mem_some10": self.psi_mem_some10,
            "psi_mem_full10": self.psi_mem_full10,
            "psi_cpu_some10": self.psi_cpu_some10,
            "cpu_busy_cores": round(self.cpu_busy_cores, 2),
        }


def _psi_some_full(path: str) -> tuple[float, float]:
    """``avg10`` of the ``some`` and ``full`` lines of a PSI file."""
    some = full = 0.0
    try:
        with open(path, "rb") as fh:
            for line in fh:
                parts = line.split()
                if not parts:
                    continue
                try:
                    avg10 = float(parts[1].split(b"=")[1])
                except (IndexError, ValueError):
                    continue
                if parts[0] == b"some":
                    some = avg10
                elif parts[0] == b"full":
                    full = avg10
    except OSError:
        # PSI absent (kernel built without it). The policy then leans on the
        # absolute memory floor alone, which is the conservative direction.
        pass
    return some, full


def read_pressure(prev: Pressure | None = None) -> Pressure:
    """The hot-path read: four files, ~0.036 ms.

    ``cpu_busy_cores`` needs two samples to mean anything; on the first call it
    is 0.0, which reads as "quiet" and simply defers the first full pass by one
    tick.
    """
    now = time.monotonic()
    mem_available = mem_free = swap_free = 0
    with open("/proc/meminfo", "rb") as fh:
        for line in fh:
            if line.startswith(b"MemAvailable:"):
                mem_available = int(line.split()[1]) * 1024
            elif line.startswith(b"MemFree:"):
                mem_free = int(line.split()[1]) * 1024
            elif line.startswith(b"SwapFree:"):
                swap_free = int(line.split()[1]) * 1024

    with open("/proc/stat", "rb") as fh:
        fields = fh.readline().split()
    vals = [int(x) for x in fields[1:11]]
    total = sum(vals)
    idle = vals[3] + vals[4]  # idle + iowait

    busy_cores = 0.0
    if prev is not None:
        p_total, p_idle = prev._stat
        d_total = total - p_total
        d_idle = idle - p_idle
        dt = now - prev.ts
        if d_total > 0 and dt > 0:
            busy_cores = (d_total - d_idle) / CLK_TCK / dt

    mem_some, mem_full = _psi_some_full("/proc/pressure/memory")
    cpu_some, _ = _psi_some_full("/proc/pressure/cpu")

    return Pressure(
        ts=now,
        mem_available_b=mem_available,
        mem_free_b=mem_free,
        swap_free_b=swap_free,
        psi_mem_some10=mem_some,
        psi_mem_full10=mem_full,
        psi_cpu_some10=cpu_some,
        cpu_busy_cores=busy_cores,
        _stat=(total, idle),
    )


def uptime_ticks() -> int:
    with open("/proc/uptime", "rb") as fh:
        return int(float(fh.read().split()[0]) * CLK_TCK)

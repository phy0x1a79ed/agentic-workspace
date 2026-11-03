"""Warn before the kernel picks an OOM victim on this node.

On 2026-10-19 a 31.9 GB python ran altair out of memory. The kill that
followed stopped the scope the Claude Code daemon lived in, and every session
went with it. The daemon now sits outside such scopes, but a box that runs out
still kills something. This watch names the biggest processes, and the
sessions that own them, while there is time to act.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from awm.claudedaemon import roster

#: Available memory below this fraction of the total counts as low.
LOW_FRACTION = float(os.environ.get("AWM_KEEP_MEMORY_LOW") or 0.12)
#: Memory must stay low this long before a warning, so a brief spike stays quiet.
SUSTAIN_S = 120.0
ALERT_EVERY_S = 3600.0
TOP_N = 3


@dataclass(frozen=True)
class Hog:
    pid: int
    rss_kb: int
    command: str
    job: str | None = None
    session: str | None = None


@dataclass
class MemWatch:
    low_since: float | None = None
    last_alert: float | None = None
    warned_jobs: dict[str, float] = field(default_factory=dict)

    def tick(self, now: float, total_kb: int, available_kb: int) -> bool:
        """True when the operator should hear about low memory now."""
        if total_kb <= 0 or available_kb / total_kb >= LOW_FRACTION:
            self.low_since = None
            return False
        if self.low_since is None:
            self.low_since = now
        if now - self.low_since < SUSTAIN_S:
            return False
        if self.last_alert is not None and now - self.last_alert < ALERT_EVERY_S:
            return False
        self.last_alert = now
        return True

    def jobs_to_warn(self, now: float, hogs: list[Hog]) -> list[Hog]:
        """The hogs whose session has not been warned within the hour."""
        out = []
        for hog in hogs:
            if hog.job and now - self.warned_jobs.get(hog.job, -ALERT_EVERY_S) >= ALERT_EVERY_S:
                self.warned_jobs[hog.job] = now
                out.append(hog)
        return out


def parse_meminfo(text: str) -> tuple[int, int]:
    """``(MemTotal, MemAvailable)`` in kB."""
    fields = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            fields[key] = int(parts[0])
    return fields.get("MemTotal", 0), fields.get("MemAvailable", 0)


def read_meminfo() -> tuple[int, int]:
    return parse_meminfo(Path("/proc/meminfo").read_text())


def _rss_kb(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _command(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return "?"
    return " ".join(raw.decode(errors="replace").split("\0")).strip()[:120] or "?"


def _ppid(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rpartition(")")[2].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def owner(pid: int) -> tuple[str | None, str | None]:
    """The background job and session name of the Claude Code session above ``pid``."""
    seen = 0
    while pid and pid > 1 and seen < 64:
        rec = roster.session_by_pid(pid)
        if rec:
            job = str(rec.get("jobId") or "") or None if rec.get("kind") == "bg" else None
            return job, str(rec.get("name") or "") or None
        pid, seen = _ppid(pid) or 0, seen + 1
    return None, None


def top_hogs(n: int = TOP_N) -> list[Hog]:
    """The ``n`` processes of this user with the most resident memory."""
    uid = os.getuid()
    sizes = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
        except OSError:
            continue
        pid = int(entry.name)
        sizes.append((_rss_kb(pid), pid))
    sizes.sort(reverse=True)
    hogs = []
    for rss, pid in sizes[:n]:
        job, session = owner(pid)
        hogs.append(Hog(pid=pid, rss_kb=rss, command=_command(pid), job=job, session=session))
    return hogs


def _gb(kb: int) -> str:
    return f"{kb / 1024 / 1024:.1f} GB"


def render(node: str, total_kb: int, available_kb: int, hogs: list[Hog]) -> str:
    lines = [f"keep on {node} (an automatic notice): memory is low, "
             f"{_gb(available_kb)} of {_gb(total_kb)} available for 2 min or more. "
             "When it runs out, the kernel kills a process. The biggest of this user's:"]
    for hog in hogs:
        who = f", in session {hog.job} ({hog.session})" if hog.job else ""
        lines.append(f"- pid {hog.pid}, {_gb(hog.rss_kb)}: {hog.command}{who}")
    return "\n".join(lines)


def session_warning(node: str, hog: Hog, available_kb: int) -> str:
    return (f"keep on {node} (an automatic notice): your process {hog.pid} holds "
            f"{_gb(hog.rss_kb)}, and the node has {_gb(available_kb)} left. When it "
            "runs out, the kernel kills a process, possibly another session. Stop it, "
            "cap it (`systemd-run --user --scope -p MemoryMax=<n>G ...`), or move the "
            "work to an HPC host.")

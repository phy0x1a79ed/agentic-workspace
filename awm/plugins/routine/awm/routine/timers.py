"""systemd user timers that fire routines (routines.md R38).

The timer only fires. Its service calls `awm routine trigger`, so the routine
service keeps the run record, the one-live-run guard and the watch to a verdict.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Callable

Systemctl = Callable[[list[str]], None]

_UNIT_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def unit_dir() -> Path:
    if env := os.environ.get("AWM_ROUTINE_UNIT_DIR"):
        return Path(env)
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def unit_name(project: str, name: str) -> str:
    return "awm-routine-" + _UNIT_SAFE.sub("_", f"{project}-{name}")


def trigger_command(project: str, name: str) -> str:
    awm = os.environ.get("AWM_ROUTINE_AWM_BIN", "awm")
    return (f"{shlex.quote(awm)} routine trigger --project {shlex.quote(project)}"
            f" --name {shlex.quote(name)} --trigger timer")


def render(project: str, name: str, on_calendar: str) -> tuple[str, str]:
    """The (.service, .timer) unit texts for one routine."""
    desc = f"awm routine {project}/{name}"
    service = (
        "[Unit]\n"
        f"Description={desc} (fires a trigger)\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart=/bin/bash -lc {shlex.quote(trigger_command(project, name))}\n"
    )
    timer = (
        "[Unit]\n"
        f"Description={desc}\n\n"
        "[Timer]\n"
        f"OnCalendar={on_calendar}\n"
        "Persistent=true\n"
        "RandomizedDelaySec=30\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def _real_systemctl(args: list[str]) -> None:
    subprocess.run(["systemctl", "--user", *args], check=True,
                   capture_output=True, text=True)


def validate_calendar(spec: str, systemctl_bin: str = "systemd-analyze") -> None:
    """Reject a bad OnCalendar spec before it reaches a unit file."""
    if not spec or "\n" in spec:
        raise ValueError("timer must be one OnCalendar expression")
    try:
        res = subprocess.run([systemctl_bin, "calendar", spec], capture_output=True,
                             text=True)
    except FileNotFoundError:
        return  # no systemd-analyze here: systemd itself rejects it at enable
    if res.returncode != 0:
        raise ValueError(f"bad OnCalendar {spec!r}: {res.stderr.strip()[:200]}")


def schedule(project: str, name: str, on_calendar: str, *,
             systemctl: Systemctl | None = None, directory: Path | None = None) -> dict:
    validate_calendar(on_calendar)
    sysctl = systemctl or _real_systemctl
    d = directory or unit_dir()
    d.mkdir(parents=True, exist_ok=True)
    unit = unit_name(project, name)
    service, timer = render(project, name, on_calendar)
    (d / f"{unit}.service").write_text(service)
    (d / f"{unit}.timer").write_text(timer)
    sysctl(["daemon-reload"])
    sysctl(["enable", "--now", f"{unit}.timer"])
    return {"unit": f"{unit}.timer", "dir": str(d), "on_calendar": on_calendar}


def unschedule(project: str, name: str, *, systemctl: Systemctl | None = None,
               directory: Path | None = None) -> dict:
    sysctl = systemctl or _real_systemctl
    d = directory or unit_dir()
    unit = unit_name(project, name)
    present = (d / f"{unit}.timer").exists()
    if present:
        try:
            sysctl(["disable", "--now", f"{unit}.timer"])
        except subprocess.CalledProcessError:
            pass  # already stopped or never loaded: the files still go
    for suffix in (".timer", ".service"):
        (d / f"{unit}{suffix}").unlink(missing_ok=True)
    if present:
        sysctl(["daemon-reload"])
    return {"unit": f"{unit}.timer", "removed": present}

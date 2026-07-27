"""Shared fixtures: a real snapshot of this box, and a synthetic tree builder.

The snapshot is the important one. Attribution and the protected set are both
things that can only be wrong *against reality* — a unit test built entirely
from invented processes will happily agree with an invented model of the world.
``tests/fixtures/proc_snapshot.json`` is 304 real processes from the shared
box, MCP servers, SSH ControlMasters, an accidentally agent-launched production
gateway and all, which is where every surprise in this service came from.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awm.compute.probe import Proc

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def snapshot() -> dict:
    return json.loads((FIXTURES / "proc_snapshot.json").read_text())


@pytest.fixture(scope="session")
def snapshot_procs(snapshot) -> dict[int, Proc]:
    out = {}
    for row in snapshot["procs"]:
        fields = {k: v for k, v in row.items() if k not in ("cmdline", "session")}
        out[row["pid"]] = Proc(**fields)
    return out


@pytest.fixture(scope="session")
def snapshot_cmdlines(snapshot) -> dict[int, str]:
    return {r["pid"]: r["cmdline"] for r in snapshot["procs"]}


@pytest.fixture(scope="session")
def snapshot_sessions(snapshot) -> dict[int, str | None]:
    return {r["pid"]: r["session"] for r in snapshot["procs"]}


def mkproc(
    pid: int,
    ppid: int = 1,
    *,
    start: int = 100,
    utime: int = 0,
    stime: int = 0,
    cutime: int = 0,
    cstime: int = 0,
    rss_pages: int = 0,
    pgid: int | None = None,
    comm: str = "x",
) -> Proc:
    return Proc(
        pid=pid, ppid=ppid, pgid=pid if pgid is None else pgid, comm=comm,
        state="S", utime=utime, stime=stime, cutime=cutime, cstime=cstime,
        num_threads=1, start_ticks=start, rss_pages=rss_pages, nice=0,
    )


def tree(*procs: Proc) -> dict[int, Proc]:
    return {p.pid: p for p in procs}

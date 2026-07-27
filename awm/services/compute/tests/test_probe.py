"""``/proc`` parsing — mostly about the one field that bites everybody."""

from __future__ import annotations

import os

import pytest

from awm.compute import probe

pytestmark = pytest.mark.smoke


def test_stat_parses_own_process():
    p = probe.read_stat(os.getpid())
    assert p is not None
    assert p.pid == os.getpid()
    assert p.ppid == os.getppid()
    assert p.rss_bytes > 0
    assert p.start_ticks > 0


def test_stat_of_dead_pid_is_none():
    # PID 2^22 is above the default pid_max; nothing can be there.
    assert probe.read_stat(4194303 + 1) is None


def test_comm_with_spaces_and_parens_does_not_shift_fields(tmp_path, monkeypatch):
    """The classic /proc/stat bug: comm is unescaped and may contain ')'.

    Splitting on the first ')' — or on whitespace — silently shifts every
    subsequent field, so a process named ``foo (bar) baz`` would report someone
    else's CPU and RSS. Split on the LAST ')'.
    """
    # Fields 3..24 of /proc/<pid>/stat, in order.
    fields = [
        "S", "42", "7", "0", "0", "0", "0", "0", "0", "0", "0",   # 3..13
        "11", "12", "13", "14",                                   # u/s/cu/cs
        "0", "3", "20", "0", "9999", "0", "5555",                 # ..rss
    ]
    raw = "1234 (weird (name) here) " + " ".join(fields) + "\n"
    fake = tmp_path / "1234"
    fake.mkdir()
    (fake / "stat").write_text(raw)

    real_open = open

    def fake_open(path, *a, **kw):
        if str(path) == "/proc/1234/stat":
            return real_open(fake / "stat", *a, **kw)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)
    p = probe.read_stat(1234)
    assert p is not None
    assert p.comm == "weird (name) here"
    assert p.ppid == 42
    assert p.pgid == 7
    assert (p.utime, p.stime, p.cutime, p.cstime) == (11, 12, 13, 14)
    assert p.nice == 3
    assert p.num_threads == 20
    assert p.start_ticks == 9999
    assert p.rss_pages == 5555


def test_total_ticks_includes_reaped_children():
    from tests.conftest import mkproc

    p = mkproc(1, utime=10, stime=5, cutime=100, cstime=50)
    assert p.own_ticks == 15
    assert p.reaped_ticks == 150
    assert p.total_ticks == 165


def test_environ_of_self_is_readable():
    env = probe.read_environ(os.getpid())
    assert env.get("PATH")


def test_pss_swap_of_self():
    got = probe.read_pss_swap(os.getpid())
    assert got is not None
    pss, swap = got
    assert pss > 0 and swap >= 0
    # The whole reason this is the decision number: proportional memory is
    # never larger than resident memory.
    stat = probe.read_stat(os.getpid())
    assert stat is not None
    assert pss <= stat.rss_bytes * 1.05


def test_pid_alive_rejects_recycled_pid():
    me = os.getpid()
    p = probe.read_stat(me)
    assert p is not None
    assert probe.pid_alive(me, p.start_ticks)
    assert not probe.pid_alive(me, p.start_ticks + 1)

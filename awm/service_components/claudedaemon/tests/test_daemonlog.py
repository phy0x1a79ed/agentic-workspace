"""Reading the daemon's starts and stops out of daemon.log."""

from __future__ import annotations

from awm.claudedaemon import daemonlog

LOG = """\
[2026-10-19T21:34:28.606Z] [supervisor] shutting down (cause=upgrade, uptime=7322s, leases=0, live_workers=2)
[2026-10-19T21:34:39.307Z] [supervisor] ─── daemon start ─── version=2.1.304 pid=299292 origin=service
[2026-10-19T22:00:00.000Z] [bg] something else
[2026-10-20T02:40:43.397Z] [supervisor] ─── daemon start ─── version=2.1.304 pid=3790938 origin=transient
[2026-10-20T03:00:00.000Z] [supervisor] shutting down (cause=signal, uptime=1157s, leases=1, live_workers=15)
"""


def test_history_names_why_each_daemon_before_a_start_stopped():
    events = daemonlog.history(LOG)
    assert events[0]["after"] == {"at": "2026-10-19T21:34:28.606Z", "cause": "upgrade",
                                  "uptime_s": 7322}
    assert events[0]["down_s"] == 11
    assert events[1]["pid"] == 3790938 and events[1]["after"]["cause"] == "killed"
    assert events[2]["pid"] is None and events[2]["after"]["cause"] == "signal"


def test_summary_of_a_daemon_that_stopped_and_has_not_returned():
    out = daemonlog.summary(None, LOG)
    assert out == {"state": "down", "pid": None, "since": "2026-10-20T03:00:00.000Z",
                   "cause": "signal"}


def test_summary_of_a_daemon_killed_without_a_word():
    text = LOG.rsplit("\n", 2)[0] + "\n"
    out = daemonlog.summary(None, text)
    assert out["state"] == "down" and out["cause"] == "killed"


def test_summary_of_a_running_daemon():
    text = LOG.rsplit("\n", 2)[0] + "\n"
    out = daemonlog.summary(3790938, text)
    assert out == {"state": "up", "pid": 3790938, "since": "2026-10-20T02:40:43.397Z",
                   "cause": "killed"}

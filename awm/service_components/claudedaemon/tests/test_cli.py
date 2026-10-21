"""Ending a job through the claude CLI, with a fake runner in place of it."""
from __future__ import annotations

import subprocess

import pytest

from awm.claudedaemon import cli


class Probe:
    """A job that is gone once `rm` has run `gone_after_rm` times, its worker after `alive_for` polls."""

    def __init__(self, gone_after_rm=1, alive_for=0, run=None):
        self.gone_after_rm = gone_after_rm
        self.alive_for = alive_for
        self.run = run

    def worker_alive(self, job):
        self.alive_for -= 1
        return self.alive_for >= 0

    def present(self, job):
        rms = sum(1 for c in self.run.calls if c[1] == "rm")
        return self.gone_after_rm is None or rms < self.gone_after_rm


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _stop(job, run, probe=None, **kw):
    clock = Clock()
    probe = probe or Probe(run=run)
    probe.run = run
    return cli.stop_and_remove(job, claude=kw.pop("claude", "c"), run=run, probe=probe,
                               sleep=clock.sleep, clock=clock, **kw)


class Runner:
    def __init__(self, results):
        self.results = dict(results)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        code, out = self.results.get(argv[1], (0, b""))
        return subprocess.CompletedProcess(argv, code, stdout=out)


def test_stop_then_remove_in_that_order():
    run = Runner({})
    out = _stop("abcd1234", run, claude="/bin/claude")
    assert run.calls == [["/bin/claude", "stop", "abcd1234"],
                         ["/bin/claude", "rm", "abcd1234"]]
    assert out["stopped"] and out["removed"]


def test_an_already_stopped_job_is_still_removed():
    run = Runner({"stop": (1, b"not running")})
    out = _stop("abcd1234", run)
    assert out["removed"] is True and out["stopped"] is False


def test_a_failed_removal_carries_the_cli_output():
    run = Runner({"rm": (1, b"no such job\n")})
    out = _stop("abcd1234", run, Probe(gone_after_rm=None))
    assert out["removed"] is False and out["detail"] == "no such job"
    assert [c[1] for c in run.calls] == ["stop"] + ["rm"] * cli.RM_ATTEMPTS


def test_a_record_the_settling_daemon_writes_back_is_removed_again():
    run = Runner({})
    out = _stop("abcd1234", run, Probe(gone_after_rm=2))
    assert out["removed"] is True
    assert [c[1] for c in run.calls] == ["stop", "rm", "rm"]


def test_rm_waits_for_the_worker_to_exit():
    order = []
    run = Runner({})
    probe = Probe(alive_for=3)
    real = probe.worker_alive

    def alive(job):
        order.append("alive" if real(job) else "exited")
        return order[-1] == "alive"
    probe.worker_alive = alive
    _stop("abcd1234", run, probe)
    assert order == ["alive", "alive", "alive", "exited"]
    assert [c[1] for c in run.calls] == ["stop", "rm"]


def test_an_already_removed_job_counts_as_removed():
    run = Runner({"stop": (1, b"No job matching"), "rm": (1, b"No job matching 'abcd1234'")})
    out = _stop("abcd1234", run, Probe(gone_after_rm=0))
    assert out["removed"] is True


def test_the_probe_reads_the_jobs_dir_and_the_roster(tmp_path):
    import json
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps({"workers": {"abcd1234": {"pid": 1, "procStart": "x"}}}))
    probe = cli.JobProbe(roster_path=roster_path, jobs_dir=tmp_path / "jobs")
    assert probe.present("abcd1234") and not probe.worker_alive("abcd1234")
    roster_path.write_text(json.dumps({"workers": {}}))
    assert not probe.present("abcd1234")
    (tmp_path / "jobs" / "abcd1234").mkdir(parents=True)
    assert probe.present("abcd1234")


def test_a_timeout_is_a_failure_not_an_exception():
    class run:
        calls = []

        def __new__(cls, argv, **kw):
            raise subprocess.TimeoutExpired(argv, 1)

    out = _stop("abcd1234", run, Probe(gone_after_rm=None))
    assert out["removed"] is False and "timed out" in out["detail"]


@pytest.mark.parametrize("job", ["", "--help", "ABCD1234", "abc", None])
def test_only_a_job_id_reaches_the_cli(job):
    run = Runner({})
    with pytest.raises(ValueError):
        _stop(job, run)
    assert run.calls == []

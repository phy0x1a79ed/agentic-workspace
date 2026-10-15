"""Ending a job through the claude CLI, with a fake runner in place of it."""
from __future__ import annotations

import subprocess

import pytest

from awm.claudedaemon import cli


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
    out = cli.stop_and_remove("abcd1234", claude="/bin/claude", run=run)
    assert run.calls == [["/bin/claude", "stop", "abcd1234"],
                         ["/bin/claude", "rm", "abcd1234"]]
    assert out["stopped"] and out["removed"]


def test_an_already_stopped_job_is_still_removed():
    run = Runner({"stop": (1, b"not running")})
    out = cli.stop_and_remove("abcd1234", claude="c", run=run)
    assert out["removed"] is True and out["stopped"] is False


def test_a_failed_removal_carries_the_cli_output():
    run = Runner({"rm": (1, b"no such job\n")})
    out = cli.stop_and_remove("abcd1234", claude="c", run=run)
    assert out["removed"] is False and out["detail"] == "no such job"


def test_a_timeout_is_a_failure_not_an_exception():
    def run(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)

    out = cli.stop_and_remove("abcd1234", claude="c", run=run)
    assert out["removed"] is False and "timed out" in out["detail"]


@pytest.mark.parametrize("job", ["", "--help", "ABCD1234", "abc", None])
def test_only_a_job_id_reaches_the_cli(job):
    run = Runner({})
    with pytest.raises(ValueError):
        cli.stop_and_remove(job, claude="c", run=run)
    assert run.calls == []

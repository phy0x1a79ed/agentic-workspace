"""Attribution: inheritance, the pid-reuse guard, and the live snapshot."""

from __future__ import annotations

import pytest

from awm.compute.attribution import SESSION_VAR, Attributor

from tests.conftest import mkproc, tree

pytestmark = pytest.mark.smoke


def _attributor(envs: dict[int, dict[str, str]], monkeypatch) -> Attributor:
    monkeypatch.setattr(
        "awm.compute.attribution.read_environ",
        lambda pid: envs.get(pid, {}),
    )
    return Attributor()


def test_descendants_inherit_from_the_shell(monkeypatch):
    procs = tree(
        mkproc(10, ppid=1, start=100),                 # the shell
        *[mkproc(100 + i, ppid=10, start=200 + i) for i in range(50)],
    )
    att = _attributor({10: {SESSION_VAR: "sess-a"}}, monkeypatch)
    got = att.resolve_all(procs)
    assert all(v == "sess-a" for v in got.values())


def test_a_nested_agent_is_filed_under_its_own_session(monkeypatch):
    """One agent spawning another is the case inheritance-first gets wrong.

    The inner session's processes descend from the outer agent's shell, so
    inheriting would file everything the child agent does under its parent —
    and then act on the wrong session, and tell the wrong agent why.
    """
    procs = tree(
        mkproc(10, ppid=1, start=100),                 # outer agent's shell
        mkproc(11, ppid=10, start=150),                # nested `claude`
        mkproc(12, ppid=11, start=200),                # the nested job
    )
    att = _attributor(
        {10: {SESSION_VAR: "outer"}, 11: {SESSION_VAR: "inner"}}, monkeypatch)
    got = att.resolve_all(procs)
    assert got == {10: "outer", 11: "inner", 12: "inner"}


def test_a_job_that_scrubs_its_environment_is_still_attributed(monkeypatch):
    """Inheritance is not only cheaper than reading the environment — it is
    strictly more robust, which is why it comes first."""
    procs = tree(mkproc(10, ppid=1), mkproc(11, ppid=10, start=150))
    att = _attributor({10: {SESSION_VAR: "sess-a"}, 11: {}}, monkeypatch)
    assert att.resolve_all(procs)[11] == "sess-a"


def test_unattributed_process_stays_none(monkeypatch):
    """The user's own shell work must never come into scope."""
    procs = tree(mkproc(10, ppid=1), mkproc(11, ppid=10, start=150))
    att = _attributor({}, monkeypatch)
    assert set(att.resolve_all(procs).values()) == {None}


def test_recycled_parent_pid_does_not_leak_attribution(monkeypatch):
    """A parent that started AFTER its child is a recycled pid, not a parent.

    Without the start-time comparison, a fresh process landing on a number an
    agent's shell used to hold would adopt that agent's whole identity.
    """
    procs = tree(
        mkproc(10, ppid=1, start=900),   # "parent" is younger than its child
        mkproc(11, ppid=10, start=100),
    )
    att = _attributor({10: {SESSION_VAR: "sess-a"}}, monkeypatch)
    got = att.resolve_all(procs)
    assert got[10] == "sess-a"
    assert got[11] is None


def test_cache_survives_and_is_pruned(monkeypatch):
    procs = tree(mkproc(10, ppid=1), mkproc(11, ppid=10, start=150))
    att = _attributor({10: {SESSION_VAR: "sess-a"}}, monkeypatch)
    att.resolve_all(procs)
    reads = att.env_reads
    att.resolve_all(procs)
    assert att.env_reads == reads  # second pass reads nothing — the cache holds

    for _ in range(4):
        att.resolve_all(tree(mkproc(10, ppid=1)))
    assert len(att._cache) <= 4  # departed pids do not accumulate


def test_cycle_in_ppid_chain_terminates(monkeypatch):
    procs = tree(mkproc(10, ppid=11, start=100), mkproc(11, ppid=10, start=100))
    att = _attributor({}, monkeypatch)
    assert att.resolve_all(procs) == {10: None, 11: None}


# -- against the real box ---------------------------------------------------


def test_live_snapshot_groups_cleanly(snapshot_sessions):
    """Sanity-check the recorded reality this service was designed against."""
    sessions = {s for s in snapshot_sessions.values() if s}
    attributed = sum(1 for s in snapshot_sessions.values() if s)
    assert len(sessions) >= 5, "expected many concurrent agent sessions"
    assert attributed >= 50
    # And the majority of the box is NOT an agent's — the watchdog is a
    # minority stakeholder in this process table, by design.
    assert attributed < len(snapshot_sessions)


def test_live_snapshot_replays_to_the_same_answer(
    snapshot_procs, snapshot_sessions, snapshot_cmdlines, monkeypatch
):
    """Replaying the snapshot through the attributor reproduces it exactly.

    The fixture's ``session`` column was produced by this same code against
    live ``/proc``; replaying it from the environment of the roots alone is
    what proves the inheritance rule, not just the environment read.
    """
    roots_env = {}
    for pid, proc in snapshot_procs.items():
        parent = snapshot_procs.get(proc.ppid)
        inherited = (
            parent is not None
            and parent.start_ticks <= proc.start_ticks
            and snapshot_sessions.get(proc.ppid)
        )
        if snapshot_sessions.get(pid) and not inherited:
            roots_env[pid] = {SESSION_VAR: snapshot_sessions[pid]}

    monkeypatch.setattr(
        "awm.compute.attribution.read_environ",
        lambda pid: roots_env.get(pid, {}),
    )
    got = Attributor().resolve_all(snapshot_procs)
    assert got == snapshot_sessions

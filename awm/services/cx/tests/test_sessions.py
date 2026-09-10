"""The predicates that decide who gets handed a session and what gets deleted.

Every case here is a session that existed on a real box. `adopted` is the one
that matters most: the pool seeded it, somebody renamed it and talked to it for
27k tokens, and its roster entry still says `<warm zorilla>`. Reading identity
from the roster would hand that conversation to the next terminal.
"""

from __future__ import annotations

import time

VERSION = "2.1.268"


def test_identity_comes_from_the_state_record_not_the_roster(by_short):
    from awm.cx import sessions

    adopted = by_short["adopted"]
    assert adopted.seed_name == "<warm zorilla>"
    assert adopted.name == "remote shell"
    assert not sessions.is_ours(adopted)


def test_a_fresh_warm_session_is_claimable(by_short):
    from awm.cx import sessions

    s = by_short["warmfresh"]
    assert sessions.is_alive(s)
    assert sessions.claimable(s, version=VERSION, now=_just_after(s))


def test_an_adopted_session_is_never_claimed_and_never_deleted(by_short):
    from awm.cx import sessions

    s = by_short["adopted"]
    now = _just_after(s)
    assert not sessions.claimable(s, version=VERSION, now=now)
    assert not sessions.removable(s, version=VERSION, now=now)


def test_a_live_session_that_was_claimed_is_left_alone(by_short):
    from awm.cx import sessions

    s = by_short["stranded"]
    now = _just_after(s)
    assert s.origin_cwd is not None
    assert not sessions.claimable(s, version=VERSION, now=now)
    assert not sessions.removable(s, version=VERSION, now=now)


def test_the_same_session_is_collected_once_its_process_is_gone(by_short):
    from awm.cx import sessions

    s = by_short["strandeddead"]
    assert not sessions.is_alive(s)
    assert sessions.removable(s, version=VERSION, now=_just_after(s))


def test_a_session_that_has_aged_out_stops_being_claimable(by_short, monkeypatch):
    from awm.cx import config, sessions

    monkeypatch.setenv("AWM_CX_ROTATE_AGE_S", "60")
    s = by_short["warmfresh"]
    old = _just_after(s) + 2 * config.rotate_age_s()
    assert not sessions.claimable(s, version=VERSION, now=old)
    assert sessions.removable(s, version=VERSION, now=old)


def test_a_session_seeded_by_a_superseded_binary_is_replaced(by_short):
    from awm.cx import sessions

    s = by_short["strandeddead"]
    assert s.cli_version != VERSION


def test_an_unresolvable_binary_fails_closed_on_claiming(by_short):
    from awm.cx import sessions

    s = by_short["warmfresh"]
    assert not sessions.claimable(s, version=None, now=_just_after(s))


def test_an_unresolvable_binary_does_not_make_everything_removable(by_short):
    """Failing closed both ways would have the pool delete itself on a loop."""
    from awm.cx import sessions

    s = by_short["warmfresh"]
    assert not sessions.removable(s, version=None, now=_just_after(s))


def test_a_recycled_pid_does_not_pass_for_a_live_session(by_short):
    import dataclasses

    from awm.cx import sessions

    s = dataclasses.replace(by_short["warmfresh"], repl_proc_start="1")
    assert not sessions.is_alive(s)


def test_the_daemon_precondition_reads_the_roster_supervisor(box):
    import os

    from awm.cx import sessions

    assert sessions.daemon_pid() == os.getpid()


def test_no_roster_means_no_daemon_and_no_sessions(monkeypatch, tmp_path):
    from awm.cx import sessions

    monkeypatch.setenv("AWM_CX_ROSTER", str(tmp_path / "nothing.json"))
    monkeypatch.setenv("AWM_CX_JOBS", str(tmp_path / "jobs"))
    assert sessions.daemon_pid() is None
    assert sessions.load() == []


def test_status_names_the_reason_each_session_is_unavailable(box):
    from awm.cx import pool

    rows = {r["session"]: r for r in pool.status()["sessions"]}
    assert rows["adopted"]["why_not"] == "renamed"
    assert rows["stranded"]["why_not"] == "claimed"
    assert rows["strandeddead"]["why_not"] == "gone"


def _just_after(s) -> float:
    """A clock reading a second after the session started."""
    return (s.started_at_ms or 0) / 1000.0 + 1.0


def test_seeding_is_refused_when_no_daemon_is_running(box, monkeypatch, tmp_path):
    """The protection that keeps the user's whole fleet out of awm's cgroup."""
    from awm.cx import seed

    monkeypatch.setenv("AWM_CX_ROSTER", str(tmp_path / "gone.json"))
    assert "no claude code daemon" in (seed.precondition() or "")


def test_seeding_is_refused_beside_a_claude_md(box, monkeypatch, tmp_path):
    """A moved session carries the origin's CLAUDE.md into someone's project."""
    from awm.cx import seed

    (tmp_path / "CLAUDE.md").write_text("# not here\n")
    monkeypatch.setenv("AWM_CX_SEED_DIR", str(tmp_path))
    monkeypatch.setenv("AWM_CX_CLAUDE", "/bin/sh")
    assert "CLAUDE.md" in (seed.precondition() or "")


def test_the_transient_unit_never_kills_its_own_control_group(monkeypatch):
    """`KillMode=process` is what stops a teardown taking the daemon with it."""
    from awm.cx import seed

    argv, how, _ = seed._launch_argv("<warm test>")
    if "systemd-run" not in argv[0]:
        return
    assert "--property=KillMode=process" in argv
    assert argv[argv.index("--") + 1].endswith("claude")


def test_the_removal_plan_holds_only_the_session_nobody_can_reach(box):
    """The one case removal exists for, and nothing else on a live box."""
    from awm.cx import remove

    items = remove.plan(now=_now_for(box))
    assert [i["session"] for i in items] == ["strandeddead"]
    assert items[0]["why"] == "the process is gone"


def test_the_removal_plan_is_empty_while_every_session_is_alive(box, monkeypatch):
    from awm.cx import remove, sessions

    # Drop the corpse and nothing is left to collect: the adopted session is
    # renamed, the stranded one was claimed, and the fresh one is in use.
    import json
    roster = box / "daemon" / "roster.json"
    data = json.loads(roster.read_text())
    del data["workers"]["strandeddead"]
    roster.write_text(json.dumps(data))
    assert remove.plan(now=_now_for(box)) == []


def _now_for(box) -> float:
    """A clock reading a minute after the newest captured session started."""
    import json

    roster = json.loads((box / "daemon" / "roster.json").read_text())
    newest = max(w["startedAt"] for w in roster["workers"].values())
    return newest / 1000.0 + 60.0

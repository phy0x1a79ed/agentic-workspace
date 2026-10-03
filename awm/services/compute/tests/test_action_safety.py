"""The refusal to act — protection, victim selection, and the assertions.

These are the tests that matter most. A missed violation costs a slow box for a
few minutes; a wrong kill costs the gateway, an MCP server, or a cluster
ControlMaster whose failed reconnect burns an MFA attempt. Everything here is
written from the perspective of "prove it will not do that".

The protection cases run against ``fixtures/proc_snapshot.json`` — 304 real
processes from this box — because every surprise in this service came from
reality rather than from imagination: the production gateway turned out to
carry an agent's session id (an agent started it), and so did every detached
SSH ControlMaster.
"""

from __future__ import annotations

import os

import pytest

from awm.compute.action import (
    Unsafe,
    ancestor_pids,
    assert_safe,
    protected_reason,
    select_victim,
)
from awm.compute.probe import CLK_TCK

from tests.conftest import mkproc, tree

pytestmark = pytest.mark.smoke


# -- protection -------------------------------------------------------------


@pytest.mark.parametrize("cmd,expected", [
    ("/home/tony/lib/miniforge3/envs/awm/bin/python3.14 "
     "/home/tony/lib/miniforge3/envs/awm/bin/awm-mcp", "mcp-server"),
    ("/usr/bin/node /home/tony/agentic_workspace/.awm/mcp-lazy/"
     "chrome-lazy-mcp.mjs -- chrome-devtools-mcp", "mcp-server"),
    ("python3 /home/tony/projects/remote-audio/reflect_mcp.py", "mcp-server"),
    ("/home/tony/lib/miniforge3/envs/awm/bin/python3.14 -m awm.gateway "
     "gateway serve", "awm-gateway"),
    ("python -m awm.compute.hub_adapter", "awm-service"),
    ("ssh -f -N -M -o NumberOfPasswordPrompts=1 sockeye", "protected-binary"),
    ("ssh -W sockeye.arc.ubc.ca:22 vpn_ubc", "protected-binary"),
    ("claude --session-id abc", "protected-binary"),
    ("/home/tony/.local/share/claude/versions/2.1.220 --session-id abc",
     "claude-harness"),
    ("claude daemon run", "protected-binary"),
    ("", "unreadable"),
])
def test_infrastructure_is_protected(cmd, expected):
    assert protected_reason(cmd) == expected


@pytest.mark.parametrize("cmd", [
    "/bin/bash -c source /home/tony/.claude/shell-snapshots/snapshot.sh && make -j16",
    "python train.py --batch 512",
    "ffmpeg -i in.mkv -c:v libx265 out.mkv",
    "./relay/msm_relay start",
    "cargo build --release",
])
def test_ordinary_agent_work_is_targetable(cmd):
    assert protected_reason(cmd) is None


def test_a_shell_that_merely_mentions_infrastructure_is_not_exempt():
    """A Bash tool call's whole script is its command line.

    Without the ``bash -c`` carve-out, any agent that greps for ``awm.gateway``
    — or deliberately echoes it — would make itself unkillable. That is a hole
    wide enough to walk through on purpose.
    """
    assert protected_reason(
        "/bin/bash -c grep -rn 'awm.gateway' awm/services/ && python -m uvicorn --help"
    ) is None


def test_the_shell_carve_out_costs_nothing_because_the_subtree_still_saves_it(
    monkeypatch,
):
    """A shell that really did launch infrastructure is refused on the child's
    behalf, so exempting the shell itself loses no protection."""
    procs = tree(mkproc(10, ppid=1), mkproc(11, ppid=10, start=150))
    cmds = {10: "/bin/bash -c awm dev start", 11: "python -m awm.gateway gateway serve"}
    monkeypatch.setattr("awm.compute.action.read_cmdline", lambda pid: cmds[pid])
    monkeypatch.setattr("awm.compute.action.read_stat", lambda pid: procs.get(pid))
    cand = _cand(10, procs)
    with pytest.raises(Unsafe, match="protected"):
        assert_safe(cand, "s", {10: "s", 11: "s"}, procs, self_pids=set())


def test_no_protected_process_in_the_live_snapshot_is_ever_selectable(
    snapshot_procs, snapshot_cmdlines, snapshot_sessions, monkeypatch
):
    """Sweep the whole real process table: whatever the metric, whatever the
    session, victim selection never hands back something protected."""
    monkeypatch.setattr(
        "awm.compute.action.read_cmdline", lambda pid: snapshot_cmdlines.get(pid, ""))
    by_session: dict[str, set[int]] = {}
    for pid, sid in snapshot_sessions.items():
        if sid:
            by_session.setdefault(sid, set()).add(pid)

    picked = 0
    for sid, pids in by_session.items():
        roots = sorted(p for p in pids if snapshot_procs[p].ppid not in pids)
        for metric in ("memory", "cpu"):
            cand, _ = select_victim(
                metric, pids, roots, snapshot_procs, {}, 1.0, CLK_TCK, 10 ** 9)
            if cand is None:
                continue
            picked += 1
            assert protected_reason(cand.cmdline) is None, cand.cmdline
    assert picked > 0, "the sweep must actually exercise selection"


def test_the_gateway_in_the_snapshot_carries_a_session_id_and_is_still_safe(
    snapshot_cmdlines, snapshot_sessions
):
    """The finding that justifies the whole protected list existing."""
    gateways = [
        pid for pid, cmd in snapshot_cmdlines.items()
        if "-m awm.gateway" in cmd and "gateway serve" in cmd
    ]
    assert gateways, "fixture should contain the production gateway"
    assert any(snapshot_sessions.get(pid) for pid in gateways), (
        "the gateway was started from an agent's shell, so it is attributed — "
        "which is exactly why attribution alone cannot decide what to kill"
    )
    for pid in gateways:
        assert protected_reason(snapshot_cmdlines[pid]) == "awm-gateway"


# -- victim selection -------------------------------------------------------


def _cand(pid, procs, cmd="job"):
    from awm.compute.action import Candidate
    p = procs[pid]
    return Candidate(pid=pid, start_ticks=p.start_ticks, pgid=p.pgid,
                     cmdline=cmd, n_procs=1, rss_estimate_b=0, cpu_cores=0.0,
                     age_s=1.0)


def test_selection_prefers_the_largest_and_then_the_youngest(monkeypatch):
    monkeypatch.setattr("awm.compute.action.read_cmdline", lambda pid: f"job{pid}")
    procs = tree(
        mkproc(1, ppid=0),
        mkproc(10, ppid=1, start=100, rss_pages=1000),
        mkproc(20, ppid=1, start=900, rss_pages=1000),   # same size, younger
        mkproc(30, ppid=1, start=100, rss_pages=10),
    )
    pids = {10, 20, 30}
    cand, ranked = select_victim("memory", pids, [10, 20, 30], procs, {}, 1.0,
                                 CLK_TCK, 1000)
    assert cand is not None and cand.pid == 20
    assert [c.pid for c in ranked][:2] == [20, 10]


def test_selection_targets_job_roots_not_arbitrary_descendants(monkeypatch):
    """Kill the compiler and the build spawns another; kill the shell and the
    job is over."""
    monkeypatch.setattr("awm.compute.action.read_cmdline", lambda pid: f"job{pid}")
    procs = tree(
        mkproc(1, ppid=0),
        mkproc(10, ppid=1, rss_pages=1),
        mkproc(11, ppid=10, start=150, rss_pages=10_000),   # the fat child
    )
    cand, _ = select_victim("memory", {10, 11}, [10], procs, {}, 1.0, CLK_TCK, 1000)
    assert cand is not None and cand.pid == 10
    # ...and it is credited with its whole subtree, not just its own pages.
    assert cand.rss_estimate_b > 10_000


def test_selection_returns_none_when_every_root_is_protected(monkeypatch):
    monkeypatch.setattr("awm.compute.action.read_cmdline",
                        lambda pid: "ssh -N -M host")
    procs = tree(mkproc(1, ppid=0), mkproc(10, ppid=1))
    cand, ranked = select_victim("memory", {10}, [10], procs, {}, 1.0, CLK_TCK, 1000)
    assert cand is None and len(ranked) == 1


# -- the assertions ---------------------------------------------------------


def test_refuses_when_the_process_group_reaches_outside_the_subtree(monkeypatch):
    """``killpg`` equals "kill the subtree" only when the job leads its own
    group. MCP servers share the harness's group, so this is not theoretical."""
    procs = tree(
        mkproc(10, ppid=1, pgid=7),
        mkproc(99, ppid=1, pgid=7),      # a stranger in the same group
    )
    monkeypatch.setattr("awm.compute.action.read_cmdline", lambda pid: "job")
    monkeypatch.setattr("awm.compute.action.read_stat", lambda pid: procs.get(pid))
    with pytest.raises(Unsafe, match="outside the subtree"):
        assert_safe(_cand(10, procs), "s", {10: "s"}, procs, self_pids=set())


def test_refuses_a_recycled_or_exited_target(monkeypatch):
    procs = tree(mkproc(10, ppid=1, start=100))
    monkeypatch.setattr("awm.compute.action.read_cmdline", lambda pid: "job")
    cand = _cand(10, procs)
    monkeypatch.setattr("awm.compute.action.read_stat", lambda pid: None)
    with pytest.raises(Unsafe, match="exited"):
        assert_safe(cand, "s", {10: "s"}, procs, self_pids=set())

    monkeypatch.setattr("awm.compute.action.read_stat",
                        lambda pid: mkproc(10, ppid=1, start=999))
    with pytest.raises(Unsafe, match="recycled"):
        assert_safe(cand, "s", {10: "s"}, procs, self_pids=set())


def test_refuses_when_attribution_has_moved_on(monkeypatch):
    procs = tree(mkproc(10, ppid=1))
    monkeypatch.setattr("awm.compute.action.read_cmdline", lambda pid: "job")
    monkeypatch.setattr("awm.compute.action.read_stat", lambda pid: procs.get(pid))
    with pytest.raises(Unsafe, match="no longer attributed"):
        assert_safe(_cand(10, procs), "s", {10: "other"}, procs, self_pids=set())


def test_refuses_to_signal_itself(monkeypatch):
    procs = tree(mkproc(10, ppid=1))
    monkeypatch.setattr("awm.compute.action.read_cmdline", lambda pid: "job")
    monkeypatch.setattr("awm.compute.action.read_stat", lambda pid: procs.get(pid))
    with pytest.raises(Unsafe, match="watchdog itself"):
        assert_safe(_cand(10, procs), "s", {10: "s"}, procs, self_pids={10})


def test_a_clean_job_passes_every_assertion(monkeypatch):
    procs = tree(
        mkproc(1, ppid=0, pgid=1),
        mkproc(10, ppid=1, pgid=10),
        mkproc(11, ppid=10, pgid=10, start=150),
    )
    monkeypatch.setattr("awm.compute.action.read_cmdline",
                        lambda pid: "/bin/bash -c make -j16")
    monkeypatch.setattr("awm.compute.action.read_stat", lambda pid: procs.get(pid))
    group = assert_safe(_cand(10, procs), "s", {10: "s", 11: "s"}, procs,
                        self_pids={999})
    assert {p.pid for p in group} == {10, 11}


def test_our_own_ancestors_are_all_in_the_never_touch_set():
    mine = ancestor_pids()
    assert os.getpid() in mine
    assert os.getppid() in mine
    assert len(mine) >= 2

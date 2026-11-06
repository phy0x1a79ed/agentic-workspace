"""The land queue: submit, the merger's batch, bounce, the lease and the rollout."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awm.routine import fleetconf
from awm.routine.land import RESULT_MARK, Land

from tests.conftest import git

pytestmark = pytest.mark.smoke

INSTALL = '[fleet]\nhome = "mizar"\nnodes = ["mizar"]\n'


def land(ws, tmp_path, fleet: str = INSTALL) -> Land:
    return Land("proj", workspace=ws.root, awm_dir=tmp_path / "awm",
                node="mizar", fleet=fleetconf.parse(fleet))


def submit(ln: Land, wt: Path, brief: str = "Behavior: x\n") -> dict:
    return ln.submit(wt, brief)


REFRESH = ('[refresh]\nsync = "true"\ncommand = "scripts/refresh.sh"\n')
SCRIPT = ('#!/bin/sh\necho "$1 $AWM_REFRESH_NODE" >> {log}\n'
          'echo \'{mark}{{"ok": true, "node": "mizar"}}\'\n')


def with_refresh(ws, tmp_path) -> Path:
    log = tmp_path / "refresh.log"
    ws.scope("base", {"deploy.toml": REFRESH,
                      "scripts/refresh.sh": SCRIPT.format(log=log, mark=RESULT_MARK)})
    git(ws.bare, "update-ref", "refs/heads/release", ws.rev("feat/base"))
    git(ws.bare, "update-ref", "refs/heads/dev", ws.rev("feat/base"))
    git(ws.proj / "release", "reset", "-q", "--hard", "release")
    return log


def test_two_branches_land_in_one_batch(ws, tmp_path):
    log = with_refresh(ws, tmp_path)
    ln = land(ws, tmp_path)
    a = submit(ln, ws.scope("a", {"a.txt": "a\n"}, base="release"))
    b = submit(ln, ws.scope("b", {"b.txt": "b\n"}, base="release"))
    assert a["role"] == "merger" and b["role"] == "merger"
    assert ln.run_batch(a["ticket"]) == 0
    head = ws.rev("release")
    assert ws.show("release", "a.txt") == "a" and ws.show("release", "b.txt") == "b"
    assert ws.rev("dev") == head
    assert ln.ticket(a["ticket"])["state"] == ln.ticket(b["ticket"])["state"] == "landed"
    assert log.read_text().split() == [head, "mizar"]
    assert ln.wait(b["ticket"], 0.01)["verdict"] == "landed"
    assert ln.batch() is None and ln.merger() == ""


def test_a_waiter_waits_while_another_merges(ws, tmp_path):
    with_refresh(ws, tmp_path)
    ln = land(ws, tmp_path)
    a = submit(ln, ws.scope("a", {"app.txt": "from a\n"}, base="release"))
    b = submit(ln, ws.scope("b", {"app.txt": "from b\n"}, base="release"))
    assert ln.run_batch(a["ticket"]) == 1
    assert ln.merger() == a["ticket"]
    assert submit(ln, ws.scope("c", {"c.txt": "c\n"}, base="release"))["role"] == "queued"
    assert ln.run_batch(b["ticket"]) == 3
    with pytest.raises(SystemExit):
        ln.hold(b["ticket"])


def test_conflict_stops_and_the_merger_resolves_it(ws, tmp_path):
    with_refresh(ws, tmp_path)
    ln = land(ws, tmp_path)
    a = submit(ln, ws.scope("a", {"app.txt": "from a\n"}, base="release"))
    submit(ln, ws.scope("b", {"app.txt": "from b\n"}, base="release"))
    rel0 = ws.rev("release")
    assert ln.run_batch(a["ticket"]) == 1
    assert "conflicts with the batch in app.txt" in ln.batch()["failed"]
    assert ws.rev("release") == rel0
    (ln.tree / "app.txt").write_text("from a and b\n")
    git(ln.tree, "add", "app.txt")
    git(ln.tree, "commit", "-q", "--no-edit")
    assert ln.run_batch(a["ticket"]) == 0
    assert ws.show("release", "app.txt") == "from a and b"


def test_bounce_returns_the_branch_and_lands_the_rest(ws, tmp_path):
    with_refresh(ws, tmp_path)
    ln = land(ws, tmp_path)
    a = submit(ln, ws.scope("a", {"a.txt": "a\n"}, base="release"))
    b = submit(ln, ws.scope("b", {"deploy.toml": '[deploy]\ncheck = "exit 3"\n'},
                            base="release"))
    assert ln.run_batch(a["ticket"]) == 1
    assert "check: exit 3" in ln.batch()["failed"]
    ln.bounce(b["ticket"], "its deploy.toml breaks the check", a["ticket"])
    assert ln.run_batch(a["ticket"]) == 0
    assert ws.show("release", "a.txt") == "a"
    assert ws.show("release", "deploy.toml") == REFRESH.strip()
    w = ln.wait(b["ticket"], 0.01)
    assert w["verdict"] == "bounced" and "breaks the check" in w["reason"]


def test_an_expired_lease_passes_the_batch_on(ws, tmp_path):
    with_refresh(ws, tmp_path)
    ln = land(ws, tmp_path)
    a = submit(ln, ws.scope("a", {"app.txt": "from a\n"}, base="release"))
    b = submit(ln, ws.scope("b", {"app.txt": "from b\n"}, base="release"))
    assert ln.run_batch(a["ticket"]) == 1
    lease = ln.dir / "lease.json"
    lease.write_text(json.dumps({**json.loads(lease.read_text()), "until": 0}))
    assert ln.wait(b["ticket"], 0.01)["verdict"] == "merger"


def test_a_failed_node_stops_the_rollout_and_resumes_after_it(ws, tmp_path):
    log = with_refresh(ws, tmp_path)
    fleet = ('[fleet]\nhome = "mizar"\nnodes = ["mizar", "alcor"]\n'
             '[fleet.push.alcor]\nurl = "{u}"\nprojects = ["proj"]\nrefresh = "{cmd}"\n')
    peer = tmp_path / "peer-{project}.git"
    git(tmp_path, "init", "-q", "--bare", str(tmp_path / "peer-proj.git"))
    flag = tmp_path / "alcor-ok"
    cmd = f"test -e {flag} && echo $AWM_REFRESH_NODE >> {log}"
    ln = land(ws, tmp_path, fleet.format(u=peer, cmd=cmd))
    a = submit(ln, ws.scope("a", {"a.txt": "a\n"}, base="release"))
    assert ln.run_batch(a["ticket"]) == 1
    assert "rollout alcor" in ln.batch()["failed"]
    assert [n["node"] for n in ln.batch()["nodes"]] == ["mizar", "alcor"]
    flag.touch()
    assert ln.run_batch(a["ticket"]) == 0
    # mizar refreshed once, alcor on the second run.
    assert log.read_text().split()[1:] == ["mizar", "alcor"]
    assert git(tmp_path / "peer-proj.git", "rev-parse", "refs/mizar/release") == ws.rev("release")


def test_submit_refuses_uncommitted_work_and_release(ws, tmp_path):
    ln = land(ws, tmp_path)
    wt = ws.scope("a", {"a.txt": "a\n"})
    (wt / "a.txt").write_text("dirty\n")
    with pytest.raises(SystemExit, match="commit your work"):
        submit(ln, wt)
    with pytest.raises(SystemExit, match="feature branch"):
        submit(ln, ws.proj / "release")


def test_a_node_without_its_own_command_refreshes_over_ssh(ws, tmp_path):
    from awm.routine.deployconf import parse
    fleet = ('[fleet]\nhome = "mizar"\nnodes = ["mizar", "altair"]\n'
             '[fleet.push.altair]\nurl = "altair:/r/{project}"\nprojects = ["awm"]\n')
    ln = land(ws, tmp_path, fleet)
    cfg = parse('[refresh]\nsync = "scripts/refresh-checkout.sh"\n'
                'command = "scripts/node-refresh.sh"\n')
    ln.project = "awm"
    cmd = ln._refresh_command("altair", cfg, "a" * 40, "b" * 40)
    assert cmd.startswith("ssh -o BatchMode=yes altair ")
    assert ("cd agentic_workspace && AWM_REFRESH_TO=" + "b" * 40
            + " scripts/refresh-checkout.sh && bash scripts/node-refresh.sh " + "b" * 40) in cmd

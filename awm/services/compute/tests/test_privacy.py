"""Private marks: a peer never sees a process that runs in, or names, a marked path."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from awm.config import privacy

PEER, LOCAL, USER = "peer:altair", None, "user:tony"


@pytest.fixture()
def marked(tmp_path, monkeypatch):
    import awm.config as cfg
    import awm.persistence.databases as pdbs
    monkeypatch.setattr(cfg, "AWM_DIR", tmp_path / ".awm")
    monkeypatch.setattr(pdbs, "SERVICES_DIR", tmp_path / "services")
    monkeypatch.setenv("AWM_HUB_URL", "http://127.0.0.1:7899")
    root = tmp_path / "projects"
    for name in ("avatar", "avatar2", "awm"):
        (root / name).mkdir(parents=True)
    privacy.write_marks([{"project": "avatar", "scope": None, "path": str(root / "avatar")}])
    from awm.compute import hub_adapter
    hub_adapter.WATCHER.setup()
    return root


@pytest.fixture()
def spawn():
    procs = []

    def start(*args: str, cwd) -> int:
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", *args], cwd=cwd)
        procs.append(p)
        for _ in range(250):
            with open(f"/proc/{p.pid}/cmdline", "rb") as fh:
                if b"time.sleep" in fh.read():
                    break
            time.sleep(0.02)
        return p.pid

    yield start
    for p in procs:
        p.kill()
        p.wait()


def _explain(pid, as_):
    from awm.compute.hub_adapter import HANDLERS
    return HANDLERS["explain"]({"pid": pid}, as_)


def test_explain_answers_a_peer_as_if_a_marked_process_did_not_exist(marked, spawn):
    in_marked = spawn(cwd=marked / "avatar")
    naming = spawn(str(marked / "avatar" / "PLAN.md"), cwd=marked / "awm")
    sibling = spawn(str(marked / "avatar2"), cwd=marked / "avatar2")
    for pid in (in_marked, naming, sibling):
        assert _explain(pid, LOCAL)["found"] is True
        assert _explain(pid, USER)["found"] is True
    assert _explain(in_marked, PEER) == {"pid": in_marked, "found": False}
    assert _explain(naming, PEER) == {"pid": naming, "found": False}
    assert _explain(sibling, PEER)["found"] is True
    assert _explain(os.getpid(), "peer")["found"] is True


def _record(cmdline, detail=None):
    from awm.compute import store
    store.record_decision(session_id="s", metric="cpu", kind="hard", measured=9.0, cap=4.0,
                          action="renice", outcome="dry-run", target_pid=1, cmdline=cmdline,
                          detail=detail)


def test_decisions_leave_out_rows_naming_a_marked_path_for_a_peer(marked):
    from awm.compute.hub_adapter import HANDLERS
    _record(f"make -C {marked / 'avatar'} all")
    _record(f"make -C {marked / 'avatar2'} all")
    _record("make all", {"victims": [{"cmdline": f"cc {marked / 'avatar' / 'x.c'}"}]})
    _record(f"pytest {marked / 'awm'}")

    def cmdlines(as_):
        return sorted(r["cmdline"] for r in HANDLERS["decisions"]({}, as_)["decisions"])

    assert len(cmdlines(LOCAL)) == len(cmdlines(USER)) == 4
    assert cmdlines(PEER) == sorted([f"make -C {marked / 'avatar2'} all", f"pytest {marked / 'awm'}"])
    privacy.marks_path().write_text("{not json")
    assert cmdlines(PEER) == [] and len(cmdlines(LOCAL)) == 4

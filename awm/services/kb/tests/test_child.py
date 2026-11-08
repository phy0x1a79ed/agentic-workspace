import os
import signal
import time

import pytest

from awm.kb import server


def test_child_env_replaces_pythonpath_and_names_the_server(kb_paths, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/awm/worktree")
    env = server.child_env()
    assert env["PYTHONPATH"] == str(kb_paths.CHECKOUT / "src")
    assert "LLM_API_KEY" not in env
    assert env["KB_PORT"] == str(kb_paths.PORT) and env["KB_HOST"] == "127.0.0.1"


def _wait(pred, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_lifecycle_respawn_and_hold(kb_paths):
    child = server.Child()
    try:
        res = child.start()
        assert res["action"] == "started" and res["listening"], res
        pid = res["pid"]

        os.kill(pid, signal.SIGKILL)
        assert _wait(lambda: not child._alive())
        assert child.reconcile()["action"] == "respawned"
        assert _wait(kb_paths.listening)
        assert child.snapshot()["pid"] != pid

        assert child.stop(hold=True)["action"] == "stopped"
        assert child.reconcile() == {"action": "held"}
        assert not _wait(kb_paths.listening, 1.0)
        assert child.start()["action"] == "started"
    finally:
        child.stop()


def test_missing_checkout_is_reported_not_fatal(kb_paths, tmp_path, monkeypatch):
    monkeypatch.setattr(kb_paths, "CHECKOUT", tmp_path / "nowhere")
    child = server.Child()
    with pytest.raises(FileNotFoundError):
        child.start()
    assert "no kb server" in child.snapshot()["error"]
    assert child.reconcile()["action"] == "respawn-failed"

import os
import signal
import time

import pytest

from awm.kb import server


def test_child_env_replaces_pythonpath_and_names_the_server(kb_paths, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/awm/worktree")
    monkeypatch.setenv("DEV_PYTHONPATH", "/awm/worktree")
    env = server.child_env()
    assert env["PYTHONPATH"] == str(kb_paths.CHECKOUT / "src")
    assert "DEV_PYTHONPATH" not in env
    assert "LLM_API_KEY" not in env
    assert env["KB_PORT"] == str(kb_paths.PORT) and env["KB_HOST"] == "127.0.0.1"


def test_child_env_points_the_server_at_the_shipped_indexes_that_exist(kb_paths, monkeypatch):
    monkeypatch.delenv("KB_VECTORS", raising=False)
    monkeypatch.delenv("KB_LADYBUG", raising=False)
    env = server.child_env()
    assert "KB_VECTORS" not in env
    kb_paths.VECTORS.mkdir(parents=True)
    env = server.child_env()
    assert env["KB_VECTORS"] == str(kb_paths.VECTORS) and "KB_LADYBUG" not in env
    assert "KB_ZOTERO_LIBRARY" not in env
    monkeypatch.setenv("KB_VECTORS", "/elsewhere")
    assert server.child_env()["KB_VECTORS"] == "/elsewhere"


def test_child_env_puts_the_kb_env_lib_first(kb_paths, monkeypatch):
    monkeypatch.setenv("KB_PYTHON", "/envs/kb/bin/python")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/lib")
    assert server.child_env()["LD_LIBRARY_PATH"] == os.pathsep.join(["/envs/kb/lib", "/opt/lib"])


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

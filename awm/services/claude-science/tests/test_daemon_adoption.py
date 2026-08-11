"""Adoption is the behaviour that makes an awm deploy safe for live work.

If `start` ever decides to launch a second daemon beside a running one, or
`reconcile` ever restarts one that is merely busy, the cost lands on somebody
mid-analysis. Both are pinned here, along with the origin derivation the
workbench's WebSocket origin check depends on — that one fails closed and
silently, so it is worth a test rather than a comment.
"""

from __future__ import annotations

import pytest

from awm.claude_science import daemon

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


@pytest.fixture()
def sup():
    return daemon.Supervisor(main_front_port=12201, sandbox_front_port=12202)


def _running(**over):
    base = {"running": True, "pid": 4242, "version": "0.1.27", "port": 12203}
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Adoption
# ---------------------------------------------------------------------------

def test_start_adopts_a_running_daemon_and_launches_nothing(sup, monkeypatch):
    monkeypatch.setattr(daemon, "installed", lambda: True)
    monkeypatch.setattr(daemon, "status_json", _running)
    monkeypatch.setattr(daemon.subprocess, "run", _never_called)

    snap = sup.start()
    assert snap["adopted"] is True
    assert snap["pid"] == 4242
    # Started-by-us is a different fact from running, and status must not lie
    # about which one it is.
    assert snap["supervised_since"] is None


def test_start_is_idempotent(sup, monkeypatch):
    monkeypatch.setattr(daemon, "installed", lambda: True)
    monkeypatch.setattr(daemon, "status_json", _running)
    monkeypatch.setattr(daemon.subprocess, "run", _never_called)
    assert sup.start()["adopted"] is True
    assert sup.start()["adopted"] is True


def test_reconcile_leaves_a_running_daemon_alone(sup, monkeypatch):
    monkeypatch.setattr(daemon, "installed", lambda: True)
    monkeypatch.setattr(daemon, "status_json", _running)
    monkeypatch.setattr(daemon.subprocess, "run", _never_called)
    assert sup.reconcile() == {"action": "ok"}
    assert sup.restarts == 0


def test_reconcile_does_nothing_when_the_binary_is_absent(sup, monkeypatch):
    monkeypatch.setattr(daemon, "installed", lambda: False)
    monkeypatch.setattr(daemon.subprocess, "run", _never_called)
    assert sup.reconcile()["action"] == "skipped"


def test_start_without_the_binary_says_so(sup, monkeypatch):
    monkeypatch.setattr(daemon, "installed", lambda: False)
    with pytest.raises(RuntimeError, match="not installed"):
        sup.start()


def test_stop_on_an_already_stopped_daemon_does_not_shell_out(sup, monkeypatch):
    monkeypatch.setattr(daemon, "installed", lambda: True)
    monkeypatch.setattr(daemon, "status_json", lambda: {"running": False})
    monkeypatch.setattr(daemon.subprocess, "run", _never_called)
    assert sup.stop()["running"] is False


def _never_called(*args, **kwargs):
    raise AssertionError("the supervisor must not spawn a process here")


# ---------------------------------------------------------------------------
# Origins
# ---------------------------------------------------------------------------

def test_origin_comes_from_the_declared_edge_url(monkeypatch):
    """Declared, not enumerated: on the production node the mesh address
    belongs to the Windows host and is invisible from inside WSL."""
    monkeypatch.delenv("CLAUDE_SCIENCE_PUBLIC_HOST", raising=False)
    monkeypatch.setenv("AWM_EDGE_URL", "https://10.74.81.110:12100")
    assert daemon.front_origin(12201) == "https://10.74.81.110:12201"


def test_explicit_public_host_wins(monkeypatch):
    monkeypatch.setenv("AWM_EDGE_URL", "https://10.74.81.110:12100")
    monkeypatch.setenv("CLAUDE_SCIENCE_PUBLIC_HOST", "workbench.local")
    assert daemon.front_origin(12202) == "https://workbench.local:12202"


def test_extra_origins_are_appended_and_deduped(monkeypatch):
    monkeypatch.delenv("CLAUDE_SCIENCE_PUBLIC_HOST", raising=False)
    monkeypatch.setenv("AWM_EDGE_URL", "https://10.74.81.110:12100")
    monkeypatch.setenv(
        "CLAUDE_SCIENCE_EXTRA_ORIGINS",
        " https://172.25.181.70:12201 , https://10.74.81.110:12201/ ",
    )
    assert daemon.allowed_origins(12201) == [
        "https://10.74.81.110:12201",
        "https://172.25.181.70:12201",
    ]


def test_falls_back_to_loopback_with_nothing_declared(monkeypatch):
    monkeypatch.delenv("CLAUDE_SCIENCE_PUBLIC_HOST", raising=False)
    monkeypatch.delenv("AWM_EDGE_URL", raising=False)
    monkeypatch.delenv("CLAUDE_SCIENCE_EXTRA_ORIGINS", raising=False)
    assert daemon.allowed_origins(12201) == ["https://127.0.0.1:12201"]


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------

def test_status_never_raises_on_unparseable_output(monkeypatch):
    class _Proc:
        stdout, stderr, returncode = "not json at all", "", 0

    monkeypatch.setattr(daemon, "installed", lambda: True)
    monkeypatch.setattr(daemon, "_run", lambda *a, **k: _Proc())
    st = daemon.status_json()
    assert st["running"] is False
    assert "unparseable" in st["error"]

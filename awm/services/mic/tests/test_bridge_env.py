"""The subprocess environment mic hands to ``pacat``.

Regression cover for a live outage: after mic was reduced to a pure consumer,
`pacat` inherited systemd's minimal environment, which carries no
``XDG_RUNTIME_DIR``. libpulse then cannot find ``$XDG_RUNTIME_DIR/pulse/native``
and dies with "Connection refused" on every stream — while `virtmic_status`
happily reports the daemon healthy, because virtmic repairs the variable in its
*own* process. The phone connected, `pacat` died, the socket closed, the browser
reconnected, and the whole thing spun at ~2 Hz.
"""

import os

from awm.mic.bridge import Handler


def test_pacat_env_always_carries_a_runtime_dir(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    env = Handler._pacat_env()
    assert env["XDG_RUNTIME_DIR"] == f"/run/user/{os.getuid()}"


def test_an_existing_runtime_dir_is_never_overridden(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/9999")
    assert Handler._pacat_env()["XDG_RUNTIME_DIR"] == "/run/user/9999"


def test_the_rest_of_the_environment_is_inherited(monkeypatch):
    monkeypatch.setenv("AWM_MIC_CANARY", "kept")
    assert Handler._pacat_env().get("AWM_MIC_CANARY") == "kept"

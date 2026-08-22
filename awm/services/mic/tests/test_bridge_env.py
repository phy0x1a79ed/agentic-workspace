"""The audio path mic hands to ``pacat`` — its argv and its environment.

The environment half is regression cover for a live outage: after mic was
reduced to a pure consumer, `pacat` inherited systemd's minimal environment,
which carries no ``XDG_RUNTIME_DIR``. libpulse then cannot find
``$XDG_RUNTIME_DIR/pulse/native`` and dies with "Connection refused" on every
stream — while `virtmic_status` happily reports the daemon healthy, because
virtmic repairs the variable in its *own* process. The phone connected, `pacat`
died, the socket closed, the browser reconnected, and the whole thing spun at
~2 Hz.

The argv half exists because mic moved off its private listener and onto the
hub. Everything about the transport changed; the command that actually plays the
audio into the sink had to change in no way at all, and this is what says so.
"""

import os

import pytest

from awm.mic.bridge import pacat_argv, pacat_env, parse_init


def test_pacat_env_always_carries_a_runtime_dir(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    env = pacat_env()
    assert env["XDG_RUNTIME_DIR"] == f"/run/user/{os.getuid()}"


def test_an_existing_runtime_dir_is_never_overridden(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/9999")
    assert pacat_env()["XDG_RUNTIME_DIR"] == "/run/user/9999"


def test_the_rest_of_the_environment_is_inherited(monkeypatch):
    monkeypatch.setenv("AWM_MIC_CANARY", "kept")
    assert pacat_env().get("AWM_MIC_CANARY") == "kept"


def test_the_pacat_argv_is_unchanged_by_the_move_onto_the_hub():
    assert pacat_argv("virtmic", 48000, 1) == [
        "pacat", "--playback", "-d", "virtmic", "--raw",
        "--format=s16le", "--rate=48000", "--channels=1",
        "--client-name=awm-mic", "--stream-name=browser-mic",
        "--latency-msec=40",
    ]


# -- the session-open payload ----------------------------------------------
#
# The capture format now arrives before any audio does, so a bad value has to be
# caught here: `pacat` accepts a wrong rate happily and silently pitch-shifts
# everything the machine records.


def test_a_browsers_native_rate_is_accepted():
    assert parse_init({"sampleRate": 44100, "channels": 1}) == (44100, 1)


def test_the_defaults_match_the_page():
    assert parse_init({}) == (48000, 1)


@pytest.mark.parametrize("init", [
    {"sampleRate": 0},
    {"sampleRate": 10 ** 9},
    {"sampleRate": "many"},
    {"channels": 7},
    {"format": "f32le"},
])
def test_nonsense_never_reaches_pacat(init):
    with pytest.raises(ValueError):
        parse_init(init)

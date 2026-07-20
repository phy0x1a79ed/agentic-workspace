"""Provisioning tests for the virtmic service, with ``pactl`` stubbed.

Everything goes through ``audio._run``, so a single fake process runner stands
in for ``pactl`` / ``systemctl`` / ``pulseaudio``. That lets these tests model a
real PulseAudio world (which sinks are loaded, what the default source is,
whether the daemon answers at all) without a sound server, and — because
``ensure_once`` is factored out non-sleeping — without touching the clock.

``HOME`` is redirected per-test so the config writers hit a tmp dir rather than
the developer's real ``~/.config/pulse``.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from awm.virtmic import audio


class FakePulse:
    """A stand-in PulseAudio world driven through ``audio._run``."""

    def __init__(self, *, running=True, sinks=None, default_source=None,
                 systemd=True, modules="0\tmodule-suspend-on-idle\t\n"):
        self.running = running
        self.sinks = list(sinks if sinks is not None else ["auto_null"])
        self.default_source = default_source or "auto_null.monitor"
        self.systemd = systemd
        self.modules = modules
        self.calls: list[list[str]] = []
        #: set by the test to make the daemon come back on start/restart
        self.starts_ok = True

    def _cp(self, rc=0, out=""):
        return subprocess.CompletedProcess([], returncode=rc, stdout=out, stderr="")

    def __call__(self, cmd, *, check=False):
        self.calls.append(list(cmd))
        prog = cmd[0]

        if prog == "systemctl":
            unit = cmd[-1]
            if cmd[2] == "is-active":
                active = self.systemd and unit == "pulseaudio.socket"
                return self._cp(0 if active else 3, "active\n" if active else "inactive\n")
            if cmd[2] in ("start", "restart"):
                self.running = self.starts_ok
                # A real restart reloads default.pa, which rebuilds the sink.
                if self.running and "virtmic" not in self.sinks:
                    self.sinks.append("virtmic")
                return self._cp(0)
            return self._cp(0)

        if prog == "pulseaudio":
            if "-k" in cmd:
                self.running = False
            else:
                self.running = self.starts_ok
            return self._cp(0)

        if prog != "pactl":
            return self._cp(127)
        if not self.running:
            return self._cp(1)

        sub = cmd[1]
        if sub == "info":
            return self._cp(0, "Server String: /run/user/1000/pulse/native\n")
        if sub == "get-default-source":
            return self._cp(0, self.default_source + "\n")
        if sub == "set-default-source":
            self.default_source = cmd[2]
            return self._cp(0)
        if sub == "list":
            what = cmd[-1]
            if what == "sinks":
                return self._cp(0, "".join(
                    f"{i}\t{n}\tmodule-null-sink.c\ts16le\tIDLE\n"
                    for i, n in enumerate(self.sinks)))
            if what == "modules":
                return self._cp(0, self.modules)
            return self._cp(0, "")
        if sub == "load-module":
            name = next((a.split("=", 1)[1] for a in cmd if a.startswith("sink_name=")), None)
            if name:
                self.sinks.append(name)
            return self._cp(0, "42\n")
        if sub == "unload-module":
            self.modules = ""
            return self._cp(0)
        return self._cp(0)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    # Reset the module-level rolling state so tests don't bleed into each other.
    audio._STATE.update(last_ok_ts=None, last_heal_ts=None,
                        last_error=None, passes=0, heals=0)
    return tmp_path


@pytest.fixture
def pulse(monkeypatch):
    fake = FakePulse()
    monkeypatch.setattr(audio, "_run", fake)
    return fake


# -- the config layers ------------------------------------------------------


@pytest.mark.smoke
def test_daemon_conf_disables_idle_exit(home, pulse):
    assert audio.ensure_daemon_conf() is True
    body = (home / ".config/pulse/daemon.conf").read_text()
    assert "exit-idle-time = -1" in body


@pytest.mark.smoke
def test_default_pa_declares_the_sink(home, pulse):
    assert audio.ensure_default_pa("virtmic") is True
    body = (home / ".config/pulse/default.pa").read_text()
    assert "load-module module-null-sink sink_name=virtmic" in body
    assert "set-default-source virtmic.monitor" in body


@pytest.mark.smoke
@pytest.mark.parametrize("writer,rel", [
    (audio.ensure_daemon_conf, ".config/pulse/daemon.conf"),
    (audio.ensure_default_pa, ".config/pulse/default.pa"),
    (audio.ensure_asoundrc, ".asoundrc"),
])
def test_config_writes_are_idempotent(home, pulse, writer, rel):
    """Re-running must never duplicate a stanza — the acceptance criterion."""
    assert writer() is True
    first = (home / rel).read_text()
    assert writer() is False
    assert writer() is False
    assert (home / rel).read_text() == first


@pytest.mark.smoke
def test_new_config_file_includes_the_system_copy(home, pulse, monkeypatch):
    """A user config file replaces the system one, so a file we create from
    scratch must pull the system defaults back in or we silently drop every
    module it loads."""
    monkeypatch.setattr(os.path, "exists", lambda p: p == "/etc/pulse/daemon.conf")
    audio.ensure_daemon_conf()
    body = (home / ".config/pulse/daemon.conf").read_text()
    assert ".include /etc/pulse/daemon.conf" in body
    assert body.index(".include") < body.index("exit-idle-time")


@pytest.mark.smoke
def test_existing_config_file_is_appended_not_replaced(home, pulse):
    path = home / ".config/pulse/daemon.conf"
    path.parent.mkdir(parents=True)
    path.write_text("; hand-written by the operator\ndefault-sample-rate = 48000\n")
    audio.ensure_daemon_conf()
    body = path.read_text()
    assert "default-sample-rate = 48000" in body      # preserved
    assert "exit-idle-time = -1" in body              # and ours appended after
    assert ".include" not in body                     # not a fresh file


@pytest.mark.smoke
def test_asoundrc_marker_is_byte_identical_to_the_mic_original(home, pulse):
    """Hosts that ran the old mic implementation already carry this exact
    marker; changing it would append a duplicate ALSA stanza on every one."""
    assert audio._ASOUNDRC_MARKER == "remote-audio bridge"
    (home / ".asoundrc").write_text(
        "\n# remote-audio bridge: route ALSA default through PulseAudio\n"
        "pcm.!default { type pulse }\n")
    assert audio.ensure_asoundrc() is False


# -- the reconcile pass -----------------------------------------------------


@pytest.mark.smoke
def test_ensure_once_provisions_from_scratch(home, pulse):
    res = audio.ensure_once("virtmic")
    assert res["sink_present"] is True
    assert res["default_source"] == "virtmic.monitor"
    assert res["healed"] is True
    assert "config" in res["repairs"]


@pytest.mark.smoke
def test_ensure_once_is_idempotent_on_a_healthy_host(home, pulse):
    audio.ensure_once("virtmic")
    res = audio.ensure_once("virtmic")
    assert res["healed"] is False
    assert res["repairs"] == []
    assert res["default_source"] == "virtmic.monitor"


@pytest.mark.smoke
def test_config_change_restarts_the_daemon_once(home, pulse):
    audio.ensure_once("virtmic")
    restarts = [c for c in pulse.calls if c[:3] == ["systemctl", "--user", "restart"]]
    assert len(restarts) == 1, "config write must restart PulseAudio exactly once"

    pulse.calls.clear()
    audio.ensure_once("virtmic")
    assert not [c for c in pulse.calls if "restart" in c], \
        "a steady-state pass must not restart the daemon"


@pytest.mark.smoke
def test_sink_is_rebuilt_after_pulseaudio_is_killed(home, pulse):
    """The headline behaviour: PulseAudio dies and takes the runtime-loaded
    sink with it; the next pass brings both back with no human action."""
    audio.ensure_once("virtmic")

    pulse.running = False
    pulse.sinks = []
    pulse.default_source = "auto_null.monitor"

    res = audio.ensure_once("virtmic")
    assert res["sink_present"] is True
    assert res["default_source"] == "virtmic.monitor"
    assert "daemon" in res["repairs"]


@pytest.mark.smoke
def test_sink_reloaded_when_daemon_survives_but_sink_vanished(home, pulse):
    audio.ensure_once("virtmic")
    pulse.sinks.remove("virtmic")

    res = audio.ensure_once("virtmic")
    assert res["repairs"] == ["sink", "default_source"] or "sink" in res["repairs"]
    assert "virtmic" in pulse.sinks


@pytest.mark.smoke
def test_default_source_restored_when_something_else_steals_it(home, pulse):
    audio.ensure_once("virtmic")
    pulse.default_source = "alsa_input.pci-0000_00_1f.3.analog-stereo"

    res = audio.ensure_once("virtmic")
    assert res["default_source"] == "virtmic.monitor"
    assert "default_source" in res["repairs"]


@pytest.mark.smoke
def test_unreachable_pulseaudio_raises_rather_than_reporting_healthy(home, pulse):
    """The bug this service exists to prevent: reporting ready while the audio
    path is dead."""
    pulse.running = False
    pulse.starts_ok = False
    with pytest.raises(RuntimeError, match="unreachable"):
        audio.ensure_once("virtmic")
    # Every unreachable path must record why, or status() reports last_error
    # None while broken — the exact silent failure this service exists to kill.
    assert "unreachable" in (audio.state()["last_error"] or "")


@pytest.mark.smoke
def test_heal_timestamp_only_advances_on_an_actual_repair(home, pulse):
    audio.ensure_once("virtmic")
    healed_at = audio.state()["last_heal_ts"]
    assert healed_at is not None

    audio.ensure_once("virtmic")               # no-op pass
    assert audio.state()["last_heal_ts"] == healed_at
    assert audio.state()["last_ok_ts"] >= healed_at

    pulse.sinks.remove("virtmic")
    audio.ensure_once("virtmic")               # real repair
    assert audio.state()["last_heal_ts"] >= healed_at
    assert audio.state()["heals"] == 2


@pytest.mark.smoke
def test_suspend_on_idle_is_unloaded(home, pulse):
    audio.ensure_once("virtmic")
    assert any(c[:2] == ["pactl", "unload-module"] for c in pulse.calls)


# -- the status surface -----------------------------------------------------


@pytest.mark.smoke
def test_status_reports_every_acceptance_field(home, pulse):
    audio.ensure_once("virtmic")
    st = audio.status("virtmic")
    for field in ("pulseaudio_reachable", "systemd_managed", "sink_present",
                  "default_source", "last_heal_ts", "idle_timeout_disabled",
                  "sink_declared_in_default_pa"):
        assert field in st, f"status must report {field}"
    assert st["pulseaudio_reachable"] is True
    assert st["sink_present"] is True
    assert st["default_source_ok"] is True
    assert st["idle_timeout_disabled"] is True
    assert st["sink_declared_in_default_pa"] is True


@pytest.mark.smoke
def test_status_never_repairs(home, pulse):
    """Status must be a pure read — a status call that healed would mask the
    very breakage it is meant to expose."""
    pulse.sinks = []
    st = audio.status("virtmic")
    assert st["sink_present"] is False
    assert st["default_source_ok"] is False
    assert pulse.sinks == []
    assert not any("load-module" in c for c in pulse.calls)


@pytest.mark.smoke
def test_status_on_a_dead_daemon_does_not_pretend_to_be_healthy(home, pulse):
    pulse.running = False
    st = audio.status("virtmic")
    assert st["pulseaudio_reachable"] is False
    assert st["sink_present"] is False
    assert st["default_source"] is None

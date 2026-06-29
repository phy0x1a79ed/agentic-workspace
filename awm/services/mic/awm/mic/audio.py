"""PulseAudio ``virtmic`` provisioning — the WSL virtual microphone.

A Python port of remote-audio's ``audio-setup.sh``: start a userspace
PulseAudio daemon, create a ``module-null-sink`` whose ``.monitor`` is a
recordable source, and make that monitor WSL's default capture source so
``/voice`` / ``arecord`` / the awm ``stt`` stack record the browser mic. No
WSLg / Windows audio required.

Everything here is idempotent so ``ensure_sink`` can be re-run safely from
``on_start`` or the ``mic_ensure_sink`` verb.

**Robustness note (systemd respawn):** the gateway runs services under a minimal
env with no ``XDG_RUNTIME_DIR``, so ``pactl`` / ``pacat`` can't find the user
PulseAudio socket and silently fail. ``ensure_runtime_env`` repairs that before
any PulseAudio call — the reference assumed an interactive shell.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

log = logging.getLogger("awm.mic.audio")


def ensure_runtime_env() -> None:
    """Ensure ``XDG_RUNTIME_DIR`` points at the user runtime dir.

    Under systemd respawn the service inherits a minimal env without it, so
    ``pactl``/``pacat`` would fail to reach the per-user PulseAudio socket at
    ``$XDG_RUNTIME_DIR/pulse/native``. Set on the process env so every
    subprocess we spawn inherits it.
    """
    if not os.environ.get("XDG_RUNTIME_DIR"):
        os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"


def _pactl_ok() -> bool:
    return subprocess.run(
        ["pactl", "info"], capture_output=True
    ).returncode == 0


def default_source() -> str | None:
    """The current PulseAudio default capture source, or None if unreachable."""
    ensure_runtime_env()
    r = subprocess.run(
        ["pactl", "get-default-source"], capture_output=True, text=True
    )
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def ensure_sink(sink: str = "virtmic") -> dict:
    """Idempotently provision the virtual mic and set it as the default source.

    Returns ``{"sink", "default_source"}``. Raises if PulseAudio can't be
    reached after starting it.
    """
    ensure_runtime_env()

    # 1. Ensure our own userspace PulseAudio is running.
    if not _pactl_ok():
        log.info("starting pulseaudio")
        subprocess.run(
            ["pulseaudio", "--start", "--exit-idle-time=-1"], check=False
        )
        for _ in range(25):
            if _pactl_ok():
                break
            time.sleep(0.2)
    if not _pactl_ok():
        raise RuntimeError("pulseaudio unreachable")

    # 2. Create the virtual mic: a null sink whose .monitor is recordable.
    short = subprocess.run(
        ["pactl", "list", "short", "sinks"], capture_output=True, text=True
    ).stdout
    names = [ln.split("\t")[1] for ln in short.splitlines() if "\t" in ln]
    if sink not in names:
        log.info("creating null sink %r", sink)
        subprocess.run(
            ["pactl", "load-module", "module-null-sink",
             f"sink_name={sink}",
             "sink_properties=device.description=VirtualMic"],
            check=True,
        )

    # 3. Make the monitor the default capture source so recorders see it.
    subprocess.run(
        ["pactl", "set-default-source", f"{sink}.monitor"], check=True
    )

    # 4. Stop the monitor suspending while idle (avoids a silent first second).
    mods = subprocess.run(
        ["pactl", "list", "short", "modules"], capture_output=True, text=True
    ).stdout
    for ln in mods.splitlines():
        if "module-suspend-on-idle" in ln and "\t" in ln:
            mid = ln.split("\t")[0]
            subprocess.run(["pactl", "unload-module", mid], check=False)

    # 5. Route ALSA's default device through PulseAudio so arecord sees the mic.
    _ensure_asoundrc()

    src = default_source()
    log.info("virtmic ready; default source: %s", src)
    return {"sink": sink, "default_source": src}


def _ensure_asoundrc() -> None:
    path = os.path.expanduser("~/.asoundrc")
    marker = "remote-audio bridge"
    try:
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    except FileNotFoundError:
        existing = ""
    if marker in existing:
        return
    log.info("writing %s (ALSA -> PulseAudio)", path)
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n# remote-audio bridge: route ALSA default through PulseAudio\n")
        f.write("pcm.!default { type pulse }\n")
        f.write("ctl.!default { type pulse }\n")

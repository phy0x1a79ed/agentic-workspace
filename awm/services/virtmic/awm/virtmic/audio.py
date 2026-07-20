"""PulseAudio ownership — the WSL virtual microphone (``virtmic``).

This module is the whole point of the ``virtmic`` service: it owns the
PulseAudio daemon and the ``module-null-sink`` whose ``.monitor`` is WSL's
default capture source. Anything that records — ``arecord``, ``/voice``, the
awm ``stt`` stack, the ``mic`` service's ``pacat`` bridge — reads that monitor,
so if the sink is missing they all silently record silence.

Ported from the ``mic`` service's ``audio.py`` (which provisioned the sink
exactly once at startup) and hardened into **three independent durability
layers**, each covering the others' gap:

1. **Config** (``~/.config/pulse/daemon.conf``) — ``exit-idle-time = -1`` stops
   PulseAudio exiting when the last client disconnects. This is the root cause
   of the original bug: the daemon is socket-activated by systemd and left at
   the 20-second default, so with no real audio hardware to hold a client open
   it exited constantly and took the runtime-loaded sink with it.
2. **Declaration** (``~/.config/pulse/default.pa``) — declares the null-sink in
   the daemon's own startup script, so if PulseAudio restarts *anyway* (a
   crash, a manual ``systemctl restart``, a socket re-activation) the sink is
   rebuilt by the daemon itself with no help from us.
3. **Health loop** (driven by the hub adapter, calling :func:`ensure_once`) —
   catches everything the first two miss.

Layer 1 alone would have fixed the reported symptom; layers 2 and 3 exist
because "PulseAudio never restarts" is not a property we can actually
guarantee, and the failure mode is silent.

**Robustness note (systemd respawn):** the gateway runs services under a
minimal env with no ``XDG_RUNTIME_DIR``, so ``pactl`` can't find the user
PulseAudio socket and silently fails. :func:`ensure_runtime_env` repairs that
before any PulseAudio call.

Everything here is idempotent and synchronous. The adapter runs it via
``asyncio.to_thread`` so a blocking probe never stalls the control WS; keeping
this module free of asyncio is also what lets the tests stub ``pactl`` and
never touch the clock.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

log = logging.getLogger("awm.virtmic.audio")

DEFAULT_SINK = "virtmic"

#: Idempotence markers. Each managed stanza is fenced by its marker so a
#: re-run appends nothing. ``_ASOUNDRC_MARKER`` is inherited byte-identical
#: from the old ``mic`` implementation — changing it would append a duplicate
#: stanza on every host that already ran the old code.
_ASOUNDRC_MARKER = "remote-audio bridge"
_DAEMON_MARKER = "awm virtmic: keep PulseAudio resident"
_DEFAULT_PA_MARKER = "awm virtmic: declare the virtual microphone sink"

#: Bounded so a wedged PulseAudio can't hang the health loop's worker thread.
_PACTL_TIMEOUT_S = 10.0

#: Rolling provisioning state, surfaced by the ``virtmic_status`` verb.
_STATE: dict = {
    "last_ok_ts": None,     # last pass that ended with a healthy sink
    "last_heal_ts": None,   # last pass that actually repaired something
    "last_error": None,
    "passes": 0,
    "heals": 0,
}


def state() -> dict:
    """A copy of the rolling provisioning state (for the status verb)."""
    return dict(_STATE)


# -- process plumbing -------------------------------------------------------


def ensure_runtime_env() -> None:
    """Ensure ``XDG_RUNTIME_DIR`` points at the user runtime dir.

    Under systemd respawn the service inherits a minimal env without it, so
    ``pactl`` would fail to reach the per-user PulseAudio socket at
    ``$XDG_RUNTIME_DIR/pulse/native``. Set on the process env so every
    subprocess we spawn inherits it.
    """
    if not os.environ.get("XDG_RUNTIME_DIR"):
        os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"


def _run(cmd: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    """Single seam for every subprocess this module spawns.

    Tests monkeypatch this to stub ``pactl`` / ``systemctl`` / ``pulseaudio``
    wholesale, so no test ever needs a real sound server.
    """
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_PACTL_TIMEOUT_S, check=check,
        )
    except subprocess.TimeoutExpired:
        log.warning("timed out: %s", " ".join(cmd))
        return subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr="timeout")
    except FileNotFoundError:
        log.warning("not installed: %s", cmd[0])
        return subprocess.CompletedProcess(cmd, returncode=127, stdout="", stderr="not found")


def pactl_ok() -> bool:
    """True when the PulseAudio daemon answers."""
    return _run(["pactl", "info"]).returncode == 0


def default_source() -> str | None:
    """The current PulseAudio default capture source, or None if unreachable."""
    r = _run(["pactl", "get-default-source"])
    return (r.stdout.strip() or None) if r.returncode == 0 else None


def sink_names() -> list[str]:
    """Names of the currently loaded sinks (empty if PulseAudio is down)."""
    r = _run(["pactl", "list", "short", "sinks"])
    if r.returncode != 0:
        return []
    return [ln.split("\t")[1] for ln in r.stdout.splitlines() if "\t" in ln]


def systemd_managed() -> bool:
    """True when PulseAudio is under systemd (unit or socket activation).

    Matters because a systemd-managed daemon must be restarted through
    ``systemctl``: a bare ``pulseaudio -k`` would just be socket-reactivated
    with the old config still loaded.
    """
    for unit in ("pulseaudio.service", "pulseaudio.socket"):
        r = _run(["systemctl", "--user", "is-active", unit])
        if r.stdout.strip() in ("active", "activating"):
            return True
    return False


# -- layer 1 + 2: cooperative durability via PulseAudio's own config --------


def _pulse_config_dir() -> str:
    return os.path.expanduser("~/.config/pulse")


def _append_stanza(path: str, marker: str, body: str,
                   system_include: str | None = None) -> bool:
    """Idempotently append a marker-fenced stanza to a PulseAudio config file.

    Returns True when the file was actually modified.

    ``system_include`` is the crucial subtlety: PulseAudio reads the *first*
    config file it finds, so a user-level file **replaces** the system one
    rather than layering on top of it. Creating a bare
    ``~/.config/pulse/default.pa`` with only our sink line would therefore
    silently drop every module the system file loads and break audio outright.
    When we create a file from scratch we first ``.include`` the system copy,
    then append our overrides — later assignments win, so the override lands
    without discarding anything.
    """
    try:
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    except FileNotFoundError:
        existing = None

    if existing is not None and marker in existing:
        return False

    os.makedirs(os.path.dirname(path), exist_ok=True)
    chunks: list[str] = []
    if existing is None and system_include and os.path.exists(system_include):
        chunks.append(
            f"# Created by the awm virtmic service. A user config file replaces\n"
            f"# the system one outright, so pull the system defaults back in\n"
            f"# first and override below.\n"
            f".include {system_include}\n"
        )
    elif existing and not existing.endswith("\n"):
        chunks.append("\n")

    chunks.append(f"\n### {marker}\n{body.rstrip()}\n")
    with open(path, "a", encoding="utf-8") as f:
        f.write("".join(chunks))
    log.info("wrote %s (%s)", path, marker)
    return True


def ensure_daemon_conf() -> bool:
    """Layer 1 — disable PulseAudio's idle exit. True if the file changed.

    Note this does **not** affect the *running* daemon; the caller restarts it
    once when this returns True.
    """
    return _append_stanza(
        os.path.join(_pulse_config_dir(), "daemon.conf"),
        _DAEMON_MARKER,
        "# Without this PulseAudio exits ~20s after the last client\n"
        "# disconnects, destroying the runtime-loaded virtmic sink.\n"
        "exit-idle-time = -1\n",
        system_include="/etc/pulse/daemon.conf",
    )


def ensure_default_pa(sink: str = DEFAULT_SINK) -> bool:
    """Layer 2 — declare the null-sink in the daemon's startup script.

    True if the file changed. This is what rebuilds the sink if PulseAudio
    restarts despite layer 1.
    """
    return _append_stanza(
        os.path.join(_pulse_config_dir(), "default.pa"),
        _DEFAULT_PA_MARKER,
        f"# Rebuilds the virtual microphone on every daemon start, so a\n"
        f"# restart can't leave recorders reading a dead default source.\n"
        f"load-module module-null-sink sink_name={sink} "
        f"sink_properties=device.description=VirtualMic\n"
        f"set-default-source {sink}.monitor\n",
        system_include="/etc/pulse/default.pa",
    )


def ensure_asoundrc() -> bool:
    """Route ALSA's default device through PulseAudio so ``arecord`` sees the
    mic. True if the file changed."""
    return _append_stanza(
        os.path.expanduser("~/.asoundrc"),
        _ASOUNDRC_MARKER,
        "pcm.!default { type pulse }\nctl.!default { type pulse }\n",
    )


def ensure_config(sink: str = DEFAULT_SINK) -> bool:
    """Write all three config files idempotently.

    Returns True if any of them changed — meaning the running daemon predates
    the new config and must be restarted once for it to take effect.
    """
    # Deliberately not short-circuited: every file must be evaluated.
    changed_daemon = ensure_daemon_conf()
    changed_default = ensure_default_pa(sink)
    ensure_asoundrc()  # ALSA-side only; no daemon restart needed
    return changed_daemon or changed_default


# -- daemon lifecycle -------------------------------------------------------


def _wait_reachable(timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pactl_ok():
            return True
        time.sleep(0.2)
    return pactl_ok()


def start_pulse() -> bool:
    """Start PulseAudio if it isn't answering. True once reachable."""
    if pactl_ok():
        return True
    log.info("pulseaudio unreachable; starting it")
    if systemd_managed():
        _run(["systemctl", "--user", "start", "pulseaudio.service"])
    else:
        _run(["pulseaudio", "--start", "--exit-idle-time=-1"])
    return _wait_reachable()


def restart_pulse() -> bool:
    """Restart PulseAudio so freshly-written config takes effect.

    Called at most once, immediately after :func:`ensure_config` reports a
    change. The health loop treats this as part of the same provisioning pass
    rather than a fault, so the restart it causes is never mistaken for the
    flapping it exists to prevent.
    """
    log.info("restarting pulseaudio to apply new config")
    if systemd_managed():
        _run(["systemctl", "--user", "restart", "pulseaudio.service"])
    else:
        _run(["pulseaudio", "-k"])
        time.sleep(0.3)
        _run(["pulseaudio", "--start", "--exit-idle-time=-1"])
    return _wait_reachable()


# -- the reconciling pass ---------------------------------------------------


def ensure_once(sink: str = DEFAULT_SINK) -> dict:
    """One non-sleeping reconcile pass: config → daemon → sink → default source.

    This is the whole health loop factored out pure, so tests drive it directly
    and never touch the clock. Idempotent: on an already-healthy host it makes
    no changes and reports ``healed=False``.

    Returns a status dict. Raises :class:`RuntimeError` only when PulseAudio
    cannot be reached at all — the one condition this service can't repair.
    """
    ensure_runtime_env()
    _STATE["passes"] += 1
    repairs: list[str] = []

    # Layers 1 + 2: durable config. A change means the running daemon is stale.
    if ensure_config(sink):
        repairs.append("config")
        if not restart_pulse():
            _STATE["last_error"] = "pulseaudio unreachable after config restart"
            raise RuntimeError(_STATE["last_error"])

    # The daemon itself.
    if not pactl_ok():
        repairs.append("daemon")
        if not start_pulse():
            _STATE["last_error"] = "pulseaudio unreachable"
            raise RuntimeError("pulseaudio unreachable")

    # Layer 3: the sink. Absent means PulseAudio restarted without our
    # default.pa (or predates it) — rebuild it at runtime.
    if sink not in sink_names():
        log.info("creating null sink %r", sink)
        r = _run(["pactl", "load-module", "module-null-sink",
                  f"sink_name={sink}",
                  "sink_properties=device.description=VirtualMic"])
        if r.returncode != 0:
            _STATE["last_error"] = f"load-module failed: {r.stderr.strip()}"
            raise RuntimeError(_STATE["last_error"])
        repairs.append("sink")

    # The default capture source — what every recorder actually reads.
    want = f"{sink}.monitor"
    if default_source() != want:
        log.info("setting default source to %s", want)
        _run(["pactl", "set-default-source", want])
        repairs.append("default_source")

    # Stop the monitor suspending while idle (avoids a silent first second).
    mods = _run(["pactl", "list", "short", "modules"]).stdout
    for ln in mods.splitlines():
        if "module-suspend-on-idle" in ln and "\t" in ln:
            _run(["pactl", "unload-module", ln.split("\t")[0]])
            repairs.append("suspend-on-idle")
            break

    now = time.time()
    _STATE["last_ok_ts"] = now
    _STATE["last_error"] = None
    if repairs:
        _STATE["last_heal_ts"] = now
        _STATE["heals"] += 1
        log.info("virtmic healed: %s", ", ".join(repairs))

    return {
        "sink": sink,
        "sink_present": True,
        "default_source": default_source(),
        "healed": bool(repairs),
        "repairs": repairs,
    }


def status(sink: str = DEFAULT_SINK) -> dict:
    """Read-only health snapshot — never repairs anything."""
    ensure_runtime_env()
    reachable = pactl_ok()
    names = sink_names() if reachable else []
    src = default_source() if reachable else None
    cfg_dir = _pulse_config_dir()

    def _has(path: str, marker: str) -> bool:
        try:
            with open(path, encoding="utf-8") as f:
                return marker in f.read()
        except OSError:
            return False

    return {
        "sink": sink,
        "pulseaudio_reachable": reachable,
        "systemd_managed": systemd_managed(),
        "sink_present": sink in names,
        "default_source": src,
        "default_source_ok": src == f"{sink}.monitor",
        "idle_timeout_disabled": _has(
            os.path.join(cfg_dir, "daemon.conf"), _DAEMON_MARKER),
        "sink_declared_in_default_pa": _has(
            os.path.join(cfg_dir, "default.pa"), _DEFAULT_PA_MARKER),
        "asoundrc_bridged": _has(
            os.path.expanduser("~/.asoundrc"), _ASOUNDRC_MARKER),
        **state(),
    }

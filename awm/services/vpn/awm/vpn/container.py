"""VPN container lifecycle — the privileged work, isolated in docker.

This module is the only thing that talks to ``docker``. The awm ``vpn`` service
itself stays unprivileged: it just runs ``docker run/stop/exec/ps`` against the
local daemon. Everything privileged (openconnect/protonvpn, NET_ADMIN, the
``/dev/net/tun`` device, routing, and the fail-closed kill-switch) lives inside
the per-profile image.

Singleton model: one container per profile, named ``awm-vpn-<profile>``. The DB
row is a cache; ``docker`` is the source of truth, reconciled on every ``up`` and
``status`` — so a container that died (its tunnel dropped → in-container
supervisor exited → container exited = the kill-switch firing) is reported as
``down`` even if the DB still said ``up``.

Leak-check: the exit IP is measured *inside the container's netns*
(``docker exec`` a curl) and compared to the host's public IP. If they match — or
the in-netns curl can't reach the internet at all — the exit is **not** healthy
(either leaking through the host, or fully fail-closed). Measuring from the host
would read the host IP and falsely "pass", so the check must run in-container.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from datetime import datetime, timezone

from awm.vpn import config, dao

log = logging.getLogger("awm.vpn.container")

# IP-echo endpoint hit (over HTTPS) to learn an exit IP. Plain-text body = the IP.
_IP_ECHO = "https://api.ipify.org"
# How long to wait for a freshly-started container to bring its tunnel up.
_UP_TIMEOUT_S = 45.0
_UP_POLL_S = 2.0
# Per-docker-command wall clock.
_DOCKER_TIMEOUT_S = 30.0

_host_ip_cache: str | None = None


class VpnError(Exception):
    """A docker/VPN operation failed (actionable message for the caller)."""


def container_name(profile: str) -> str:
    return f"awm-vpn-{profile}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# -- docker plumbing -------------------------------------------------------

def _docker(args: list[str], *, env: dict | None = None,
            timeout: float = _DOCKER_TIMEOUT_S,
            check: bool = True) -> subprocess.CompletedProcess:
    """Run ``docker <args>``. Raise :class:`VpnError` on a clear failure."""
    if shutil.which("docker") is None:
        raise VpnError(
            "docker not found on PATH — the vpn service orchestrates docker; "
            "install docker and ensure the service's user can reach the daemon."
        )
    try:
        proc = subprocess.run(
            ["docker", *args],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise VpnError(f"docker {' '.join(args[:2])} timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if "permission denied" in err.lower() or "cannot connect to the docker daemon" in err.lower():
            raise VpnError(
                f"cannot reach the docker daemon ({err}). Add the service user to "
                f"the docker group or run rootless docker."
            )
        raise VpnError(f"docker {' '.join(args[:2])} failed: {err}")
    return proc


def _is_running(name: str) -> bool:
    proc = _docker(["ps", "-q", "-f", f"name=^{name}$"], check=False)
    return bool(proc.stdout.strip())


def _exists(name: str) -> bool:
    proc = _docker(["ps", "-aq", "-f", f"name=^{name}$"], check=False)
    return bool(proc.stdout.strip())


def _rm_quiet(name: str) -> None:
    _docker(["rm", "-f", name], check=False)


# -- 2FA burst (host-side) -------------------------------------------------

def arm_2fa_burst(device: str) -> bool:
    """Arm the awm ``2fa`` Duo auto-approver for one push, best-effort.

    Called on the host right before a dial, so the Duo push openconnect triggers
    is auto-approved (~1s) instead of waiting on a human phone tap. It runs here
    rather than in the container because the container can't reach the host
    gateway before its tunnel is up.

    Routed through the ``AWM_TWOFA_PEER`` selector like every other 2fa
    consumer. It used to POST our own gateway unconditionally, which on a node
    that borrows 2fa meant this one caller armed a local approver while ``ssh``
    armed the owner's — the fleet-global attempt budget only holds if every
    consumer agrees on where the singleton lives.

    Returns True on a clean call. Never raises — if 2fa is unreachable the dial
    falls back to a manual Duo approval (and likely times out).
    """
    from awm import gatewayclient

    peer = gatewayclient.peer_env("AWM_TWOFA_PEER")
    try:
        gatewayclient.call_sync_maybe_peer(
            peer, "2fa", "burst", {"device": device}, timeout=15)
    except Exception as exc:  # noqa: BLE001 — arming is best-effort by contract
        log.warning("2fa burst arm failed (device=%r, owner=%s): %s",
                    device, peer or "local", exc)
        return False
    log.info("armed 2fa burst device=%r before dial (owner=%s)",
             device, peer or "local")
    return True


# -- IP / leak check -------------------------------------------------------

def host_ip() -> str:
    """The host's public egress IP (cached) — the leak-check baseline."""
    global _host_ip_cache
    if _host_ip_cache is None:
        try:
            out = subprocess.run(
                ["curl", "-fsS", "--max-time", "10", _IP_ECHO],
                capture_output=True, text=True, timeout=15,
            )
            _host_ip_cache = out.stdout.strip() if out.returncode == 0 else ""
        except Exception:  # noqa: BLE001
            _host_ip_cache = ""
    return _host_ip_cache


def exit_ip(name: str) -> str:
    """Public IP as seen from *inside* the container's netns ('' on failure)."""
    proc = _docker(
        ["exec", name, "curl", "-fsS", "--max-time", "10", _IP_ECHO],
        check=False, timeout=20,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def leak_check(name: str) -> tuple[str, bool]:
    """Return (exit_ip, healthy). Healthy = reachable in-netns AND ≠ host IP."""
    ip = exit_ip(name)
    if not ip:
        return "", False  # fail-closed: no egress through the tunnel
    host = host_ip()
    # If we couldn't learn the host IP, we can't prove no-leak; require a
    # non-empty exit IP but flag the ambiguity by still passing (best effort).
    healthy = (ip != host) if host else True
    return ip, healthy


# -- descriptor ------------------------------------------------------------

def descriptor(profile: str, *, status: str, exit_ip: str = "") -> dict:
    """The self-documenting result: how to route a process through this exit."""
    name = container_name(profile)
    attach = f"--network=container:{name}"
    port = config.bounce_port(profile)
    key = config.bounce_key_path()
    usage = (
        f"VPN exit '{profile}' "
        + ("is UP. " if status == "up" else f"is {status}. ")
        + f"Route a process through it two ways: (1) netns attach — "
        f"`docker run {attach} <image> …` (or `network_mode: \"container:{name}\"` "
        f"in compose), e.g. rlm_browser.acquire{{game, opts:{{vpn:'{profile}'}}}}; "
        f"(2) SSH ProxyJump/-W through the bounce sshd at localhost:{port} "
        f"(user root, key {key}) — e.g. `ssh -W host:port -i {key} -p {port} root@localhost`."
    )
    return {
        "profile": profile,
        "container": name,
        "attach": attach,
        "network_mode": f"container:{name}",
        "exit_ip": exit_ip,
        "status": status,
        "bounce_host": "localhost",
        "bounce_port": port,
        "bounce_user": "root",
        "bounce_key": str(key),
        "usage": usage,
    }


# -- lifecycle -------------------------------------------------------------

def up(profile: str) -> dict:
    """Bring profile's exit up (idempotent singleton). Return its descriptor.

    If the container is already running, reconcile + return without starting a
    second one. Otherwise start it, wait for the tunnel + a passing leak-check,
    persist, and return. Raises :class:`VpnError` (or
    :class:`config.VpnConfigError`) on failure.
    """
    if profile not in config.PROFILES:
        raise VpnError(f"unknown profile {profile!r}; known: {', '.join(config.PROFILES)}")
    name = container_name(profile)
    d = dao.VpnDAO()

    # Idempotent: a live container short-circuits (re-measure exit IP for status).
    if _is_running(name):
        ip, healthy = leak_check(name)
        status = "up" if healthy else "degraded"
        d.upsert(profile, container=name, status=status, exit_ip=ip)
        return descriptor(profile, status=status, exit_ip=ip)

    # Clear a stale exited container left from a crash/kill-switch trip.
    if _exists(name):
        _rm_quiet(name)

    cfg = config.load_profile(profile)  # raises if creds missing/malformed

    # Arm the local 2fa Duo auto-approver right before dialing (UBC's Duo push).
    # Cold path only — an already-running exit short-circuited above, so we never
    # fire a needless burst on a no-op `up`.
    if cfg.twofa_device:
        arm_2fa_burst(cfg.twofa_device)

    # Pass creds via `-e KEY` (value taken from our env, not argv) so secrets
    # don't show up in `docker inspect`/`ps`. NET_ADMIN + tun for the dialer;
    # publish the bounce sshd so host-side `ssh -W`/ProxyJump can egress here.
    run_env = {**_subprocess_env(), **cfg.env}
    args = ["run", "-d", "--name", name,
            "--cap-add=NET_ADMIN", "--device=/dev/net/tun",
            "-p", f"{config.bounce_port(profile)}:22"]
    for key in cfg.env:
        args += ["-e", key]
    args.append(cfg.image)
    _docker(args, env=run_env)

    # Wait for the tunnel: poll the in-netns leak-check until healthy or timeout.
    deadline = time.monotonic() + _UP_TIMEOUT_S
    last_ip = ""
    while time.monotonic() < deadline:
        if not _is_running(name):
            logs = _tail_logs(name)
            _rm_quiet(name)
            d.upsert(profile, container=name, status="down", exit_ip="")
            raise VpnError(
                f"vpn '{profile}' container exited before the tunnel came up "
                f"(fail-closed). Recent logs:\n{logs}"
            )
        last_ip, healthy = leak_check(name)
        if healthy:
            d.upsert(profile, container=name, status="up", exit_ip=last_ip)
            return descriptor(profile, status="up", exit_ip=last_ip)
        time.sleep(_UP_POLL_S)

    logs = _tail_logs(name)
    _rm_quiet(name)
    d.upsert(profile, container=name, status="down", exit_ip="")
    raise VpnError(
        f"vpn '{profile}' did not pass the leak-check within {int(_UP_TIMEOUT_S)}s "
        f"(last exit IP {last_ip or 'none'}; host IP {host_ip() or 'unknown'}). "
        f"Recent logs:\n{logs}"
    )


def down(profile: str) -> dict:
    """Tear the exit down and clear its singleton row."""
    name = container_name(profile)
    existed = _exists(name)
    _rm_quiet(name)
    dao.VpnDAO().delete(profile)
    return {"profile": profile, "container": name, "status": "down",
            "removed": existed}


def status_one(profile: str) -> dict:
    """Reconcile one profile against docker and return its descriptor.

    Adds a transient ``just_down`` flag when the container died while the DB
    still believed it up (the kill-switch fired) — the hub_adapter reads it to
    emit a state-change event, then strips it.
    """
    name = container_name(profile)
    d = dao.VpnDAO()
    row = d.get(profile)
    if _is_running(name):
        ip, healthy = leak_check(name)
        status = "up" if healthy else "degraded"
        d.upsert(profile, container=name, status=status, exit_ip=ip)
        out = descriptor(profile, status=status, exit_ip=ip)
        out["just_down"] = False
        return out
    # Not running. If the DB thought it was up, that's a crash/kill-switch trip.
    was_up = bool(row and row["status"] in ("up", "degraded"))
    if row:
        d.upsert(profile, container=name, status="down", exit_ip="")
    out = descriptor(profile, status="down", exit_ip="")
    out["just_down"] = was_up
    return out


def status_all() -> list[dict]:
    return [status_one(p) for p in config.PROFILES]


# -- helpers ---------------------------------------------------------------

def _tail_logs(name: str, lines: int = 30) -> str:
    proc = _docker(["logs", "--tail", str(lines), name], check=False)
    return ((proc.stdout or "") + (proc.stderr or "")).strip() or "(no logs)"


def _subprocess_env() -> dict:
    import os
    return dict(os.environ)

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class HostConfig:
    host: str
    user: str
    port: int = 22
    needs_vpn: bool = False
    vpn_profile: str = ""
    twofa_device: str = ""
    # guarded: a bare `ssh <host>` is blocked (via an ~/.ssh/config ProxyCommand
    # guard) unless this service already holds a live ControlMaster. The service
    # is the sole caller allowed to create the master, so it must bypass that
    # guard with `-o ProxyCommand=none` when it lays the socket down. Set for
    # MFA-lockout-prone hosts where a stray direct connect burns an auth attempt.
    guarded: bool = False


KNOWN_HOSTS: dict[str, HostConfig] = {
    "sockeye":  HostConfig("sockeye",  "txyliu",   needs_vpn=True,  vpn_profile="ubc",  twofa_device="cwl"),
    "sockeye1": HostConfig("sockeye1", "txyliu",   needs_vpn=True,  vpn_profile="ubc",  twofa_device="cwl"),
    "sockeye2": HostConfig("sockeye2", "txyliu",   needs_vpn=True,  vpn_profile="ubc",  twofa_device="cwl"),
    "sockeye3": HostConfig("sockeye3", "txyliu",   needs_vpn=True,  vpn_profile="ubc",  twofa_device="cwl"),
    "fir":      HostConfig("fir",      "phyberos",  needs_vpn=False,                    twofa_device="alliance", guarded=True),
    "nibi": HostConfig("nibi", "phyberos", twofa_device="alliance", guarded=True),
    "trillium": HostConfig("trillium", "phyberos", twofa_device="alliance", guarded=True),
    "trillium-gpu": HostConfig("trillium-gpu", "phyberos", twofa_device="alliance", guarded=True),
}

LIVE_DIR = os.path.expanduser("~/.ssh/live_connections")
SSH_ASKPASS = os.path.expanduser("~/.ssh/awm-duo-askpass")
# Any of these in ssh's stderr means the auth phase was reached, so an MFA attempt
# may have been spent. ssh's classification and the arbiter's attribution both veto on it.
AUTH_PHASE_STDERR = (
    "Permission denied",
    "Too many authentication failures",
    "Authentication failed",
    "locked",
    "MFA",
    "Duo",
)

# Process-singleton lock: exactly one svc-ssh may run at a time — two would race
# the same account toward MFA lockout with independent in-memory state. Held via
# an flock for the process lifetime; the OS releases it on death, so there is no
# stale-file lockout (unlike a bare pidfile).
SINGLETON_PATH = os.path.expanduser("~/.ssh/awm-ssh.singleton")


def resolve_host(name: str) -> HostConfig:
    cfg = KNOWN_HOSTS.get(name)
    if cfg is None:
        raise ValueError(f"unknown host {name!r}; known: {', '.join(sorted(KNOWN_HOSTS))}")
    return cfg


def control_path(cfg: HostConfig) -> str:
    return os.path.join(LIVE_DIR, f"{cfg.host}_{cfg.port}_{cfg.user}")


def stderr_path(cfg: HostConfig) -> str:
    return os.path.join(LIVE_DIR, f"{cfg.host}.connect.stderr")


def pid_path(cfg: HostConfig) -> str:
    """Where a live connect records the pid of the ssh it spawned.

    An ssh that hangs before authenticating never creates a ControlMaster
    socket, so nothing that speaks through the socket can find it — not
    `ssh -O exit`, and not the stale-socket sweep, which can only see files
    that exist. On 2026-09-01 two such processes outlived the attempt that
    made them by minutes, each holding an armed Duo window. This file is the
    only handle a later service life has on that process.
    """
    return os.path.join(LIVE_DIR, f"{cfg.host}.master.pid")

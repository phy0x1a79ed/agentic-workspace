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
    "chamois":  HostConfig("chamois",  "tliu",      needs_vpn=True,  vpn_profile="ubc"),
    "micb0":    HostConfig("micb0",    "tliu",      needs_vpn=True,  vpn_profile="ubc"),
}

LIVE_DIR = os.path.expanduser("~/.ssh/live_connections")
SSH_ASKPASS = os.path.expanduser("~/.ssh/awm-duo-askpass")
# Circuit-breaker locks: a single failed connect drops a per-host lockfile here,
# and every later connect refuses (no VPN/2FA/ssh) until the hold lifts. Recovery
# is operator-gated and out of band (a Discord /approve window) — there is no
# self-serve verb. Kept out of LIVE_DIR so it survives socket cleanup / self-heal.
LOCK_DIR = os.path.expanduser("~/.ssh/awm-ssh-locks")

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


def control_sock_name(cfg: HostConfig) -> str:
    return f"{cfg.host}_{cfg.port}_{cfg.user}"


def lock_path(cfg: HostConfig) -> str:
    return os.path.join(LOCK_DIR, f"{cfg.host}.lock")


def stderr_path(cfg: HostConfig) -> str:
    return os.path.join(LIVE_DIR, f"{cfg.host}.connect.stderr")

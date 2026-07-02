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


KNOWN_HOSTS: dict[str, HostConfig] = {
    "sockeye":  HostConfig("sockeye",  "txyliu",   needs_vpn=True,  vpn_profile="ubc",  twofa_device="cwl"),
    "sockeye1": HostConfig("sockeye1", "txyliu",   needs_vpn=True,  vpn_profile="ubc",  twofa_device="cwl"),
    "sockeye2": HostConfig("sockeye2", "txyliu",   needs_vpn=True,  vpn_profile="ubc",  twofa_device="cwl"),
    "sockeye3": HostConfig("sockeye3", "txyliu",   needs_vpn=True,  vpn_profile="ubc",  twofa_device="cwl"),
    "fir":      HostConfig("fir",      "phyberos",  needs_vpn=False,                    twofa_device="alliance"),
    "chamois":  HostConfig("chamois",  "tliu",      needs_vpn=True,  vpn_profile="ubc"),
    "micb0":    HostConfig("micb0",    "tliu",      needs_vpn=True,  vpn_profile="ubc"),
}

LIVE_DIR = os.path.expanduser("~/.ssh/live_connections")
SSH_ASKPASS = os.path.expanduser("~/.ssh/awm-duo-askpass")


def resolve_host(name: str) -> HostConfig:
    cfg = KNOWN_HOSTS.get(name)
    if cfg is None:
        raise ValueError(f"unknown host {name!r}; known: {', '.join(sorted(KNOWN_HOSTS))}")
    return cfg


def control_path(cfg: HostConfig) -> str:
    return os.path.join(LIVE_DIR, f"{cfg.host}_{cfg.port}_{cfg.user}")


def control_sock_name(cfg: HostConfig) -> str:
    return f"{cfg.host}_{cfg.port}_{cfg.user}"

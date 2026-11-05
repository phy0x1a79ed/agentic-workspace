"""A project's deploy config: `deploy.toml` at its repo root (routines.md R27, R31, R33).

The engine reads it from the commit it is about to land, so a change to the
check deploys with the code that needs it. It names no node: the install
config holds the fleet (fleetconf, fleet.md R140).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from typing import Any

FILE = "deploy.toml"
REFRESH_KINDS = ("restart", "queue", "hot", "none")
CHECK_BUDGET = 60.0


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Refresh:
    kind: str = "none"
    command: str = ""
    sync: str = ""
    prompt: str = ""
    timeout: float = 600.0


@dataclass(frozen=True)
class DeployConfig:
    check: str = ""
    check_timeout: float = CHECK_BUDGET
    # A remote of the canonical bare repo that takes `release` after each promote,
    # fast-forward only (install.md R114).
    backup: str = ""
    refresh: Refresh = field(default_factory=Refresh)
    fixer_peer: str = "local"
    fixer_prompt: str = ""
    # Pin files, each naming a built file in this node's artifact store that the
    # rollout pushes to each push node (artifacts.py, install.md R115 item 4).
    artifact_pins: tuple[str, ...] = ()


_SECTIONS = {
    "deploy": {"check", "check_timeout"},
    "promote": {"backup"},
    "refresh": {"kind", "command", "sync", "prompt", "timeout"},
    "fixer": {"peer", "prompt"},
    "artifacts": {"pins"},
}


def parse(text: str) -> DeployConfig:
    try:
        raw: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{FILE}: {e}") from e
    for sec, body in raw.items():
        if sec == "fleet":
            raise ConfigError(f"{FILE}: [fleet] belongs in the node's install config")
        if sec not in _SECTIONS:
            raise ConfigError(f"{FILE}: unknown section [{sec}]")
        if extra := set(body) - _SECTIONS[sec]:
            raise ConfigError(f"{FILE}: unknown keys in [{sec}]: {sorted(extra)}")
    d, r = raw.get("deploy", {}), raw.get("refresh", {})
    kind = r.get("kind", "none")
    if kind not in REFRESH_KINDS:
        raise ConfigError(f"{FILE}: [refresh] kind must be one of {REFRESH_KINDS}")
    if kind == "restart" and not r.get("command"):
        raise ConfigError(f"{FILE}: a restart refresh needs a command")
    if kind == "queue" and not r.get("prompt"):
        raise ConfigError(f"{FILE}: a queue refresh needs a prompt")
    fx = raw.get("fixer", {})
    pins = raw.get("artifacts", {}).get("pins", [])
    if not isinstance(pins, list) or not all(isinstance(p, str) and p for p in pins):
        raise ConfigError(f"{FILE}: [artifacts] pins must be a list of paths")
    return DeployConfig(
        check=d.get("check", ""),
        check_timeout=float(d.get("check_timeout", CHECK_BUDGET)),
        backup=raw.get("promote", {}).get("backup", ""),
        refresh=Refresh(kind=kind, command=r.get("command", ""), sync=r.get("sync", ""),
                        prompt=r.get("prompt", ""),
                        timeout=float(r.get("timeout", 600.0))),
        fixer_peer=fx.get("peer", "local"),
        fixer_prompt=fx.get("prompt", ""),
        artifact_pins=tuple(pins))

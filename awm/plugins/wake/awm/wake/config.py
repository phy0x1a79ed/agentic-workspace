"""The node's wake table, read from ``$AWM_DIR/wake.toml``.

The fleet's Ansible renders the file from its inventory (role ``wake_node``),
so the plugin carries no MAC, address or node name of its own.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_MAC = re.compile(r"^[0-9a-f]{2}([:-][0-9a-f]{2}){5}$", re.I)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Target:
    name: str
    mac: str
    broadcast: str
    probe_host: str
    probe_port: int = 22
    wakers: tuple[str, ...] = ()
    wake_budget_s: float = 180.0


@dataclass(frozen=True)
class Standby:
    partner: str | None = None
    deadline_s: float = 300.0
    rewake_every_s: float = 120.0
    rewake_max: int = 3
    reboot_command: tuple[str, ...] = ("systemctl", "reboot")
    session_project: str = "awm"
    session_scope: str = "standby"


@dataclass(frozen=True)
class WakeConfig:
    node: str
    targets: dict[str, Target] = field(default_factory=dict)
    standby: Standby = field(default_factory=Standby)

    def target(self, name: str) -> Target:
        try:
            return self.targets[name]
        except KeyError:
            known = ", ".join(sorted(self.targets)) or "none"
            raise ConfigError(f"no wake target `{name}` on {self.node} (known: {known})") from None

    def refusal(self, name: str) -> str | None:
        """Why this node may not wake ``name``, or None when it may."""
        if name == self.node:
            return f"{self.node} cannot wake itself"
        t = self.targets.get(name)
        if t is None:
            return f"{self.node} has no wake entry for `{name}`"
        if self.node not in t.wakers:
            allowed = ", ".join(t.wakers) or "nobody"
            return f"{self.node} may not wake {name}; its wakers are {allowed} (fleet.md R139)"
        return None


def default_path() -> Path:
    if os.environ.get("AWM_WAKE_CONFIG"):
        return Path(os.environ["AWM_WAKE_CONFIG"])
    awm_dir = os.environ.get("AWM_DIR")
    if awm_dir:
        return Path(awm_dir) / "wake.toml"
    return Path.home() / "agentic_workspace" / ".awm" / "wake.toml"


def load(path: Path | None = None) -> WakeConfig:
    path = path or default_path()
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"{path} is missing; the fleet's wake_node role writes it") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    return parse(raw)


def parse(raw: dict) -> WakeConfig:
    node = raw.get("node") or os.environ.get("AWM_NODE_NAME")
    if not node:
        raise ConfigError("wake.toml needs `node`")
    targets = {}
    for name, t in (raw.get("targets") or {}).items():
        mac = str(t.get("mac", ""))
        if not _MAC.match(mac):
            raise ConfigError(f"target {name}: `{mac}` is not a MAC address")
        if not t.get("broadcast") or not t.get("probe_host"):
            raise ConfigError(f"target {name} needs `broadcast` and `probe_host`")
        targets[name] = Target(
            name=name, mac=mac.lower().replace("-", ":"),
            broadcast=str(t["broadcast"]), probe_host=str(t["probe_host"]),
            probe_port=int(t.get("probe_port", 22)),
            wakers=tuple(t.get("wakers", ())),
            wake_budget_s=float(t.get("wake_budget_s", 180)))
    s = raw.get("standby") or {}
    standby = Standby(
        partner=s.get("partner"),
        deadline_s=float(s.get("deadline_s", 300)),
        rewake_every_s=float(s.get("rewake_every_s", 120)),
        rewake_max=int(s.get("rewake_max", 3)),
        reboot_command=tuple(s.get("reboot_command", ("systemctl", "reboot"))),
        session_project=str(s.get("session_project", "awm")),
        session_scope=str(s.get("session_scope", "standby")))
    if standby.partner and standby.partner not in targets:
        raise ConfigError(f"standby partner {standby.partner} has no wake target entry")
    return WakeConfig(node=str(node), targets=targets, standby=standby)

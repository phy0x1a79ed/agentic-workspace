"""Routine definitions: one TOML file per routine, kept in its project's repo.

A definition names no node. It runs wherever it is installed (routines.md R36).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
STEP_KINDS = ("check", "condition")
DEFAULT_STEP_TIMEOUT = 600.0


class DefinitionError(ValueError):
    """A routine definition that does not validate."""


@dataclass(frozen=True)
class Step:
    name: str
    run: str
    kind: str = "check"
    timeout: float = DEFAULT_STEP_TIMEOUT
    # For a condition step: the brief for the agent it dispatches when it holds.
    prompt: str = ""


@dataclass(frozen=True)
class AgentSpec:
    """Where a dispatched session runs. Empty scope means a fresh scope per run."""
    peer: str = "local"
    scope: str = ""
    prompt: str = ""


@dataclass(frozen=True)
class Routine:
    name: str
    description: str = ""
    timer: str = ""          # systemd OnCalendar expression; empty means no timer
    call: bool = True        # triggerable by a call (Tony's "by hand = agent call")
    steps: tuple[Step, ...] = ()
    agent: AgentSpec = field(default_factory=AgentSpec)
    # A routine without a fixer posts its failure as a notice and starts no agent.
    fixer: AgentSpec | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Routine":
        return cls(
            name=d["name"],
            description=d.get("description", ""),
            timer=d.get("timer", ""),
            call=bool(d.get("call", True)),
            steps=tuple(Step(**s) for s in d.get("steps", ())),
            agent=AgentSpec(**d.get("agent", {})),
            fixer=AgentSpec(**d["fixer"]) if d.get("fixer") else None,
        )


def _agent(raw: Any, where: str) -> AgentSpec:
    if raw is None:
        return AgentSpec()
    if not isinstance(raw, dict):
        raise DefinitionError(f"[{where}] must be a table")
    unknown = set(raw) - {"peer", "scope", "prompt"}
    if unknown:
        raise DefinitionError(f"[{where}] has unknown keys: {sorted(unknown)}")
    return AgentSpec(
        peer=str(raw.get("peer", "local")) or "local",
        scope=str(raw.get("scope", "")),
        prompt=str(raw.get("prompt", "")),
    )


def parse(text: str, *, source: str = "<string>") -> Routine:
    """Parse and validate one routine definition."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise DefinitionError(f"{source}: {e}") from e

    head = raw.get("routine") or {}
    name = str(head.get("name", "")).strip()
    if not NAME_RE.fullmatch(name):
        raise DefinitionError(
            f"{source}: [routine] name must be lowercase letters, digits and hyphens")
    if "node" in head or "peer" in head:
        raise DefinitionError(
            f"{source}: a routine names no node; it runs where it is installed (R36)")

    trig = raw.get("trigger") or {}
    timer = str(trig.get("timer", "")).strip()
    call = bool(trig.get("call", True))
    if not timer and not call:
        raise DefinitionError(f"{source}: a routine needs a timer, a call trigger or both")
    if any(c in timer for c in "\n\r"):
        raise DefinitionError(f"{source}: [trigger] timer must be one line")

    steps_raw = raw.get("step") or []
    if not isinstance(steps_raw, list) or not steps_raw:
        raise DefinitionError(f"{source}: a routine needs at least one [[step]]")
    steps: list[Step] = []
    seen: set[str] = set()
    for i, s in enumerate(steps_raw):
        sname = str(s.get("name", f"step-{i + 1}"))
        if sname in seen:
            raise DefinitionError(f"{source}: duplicate step name {sname!r}")
        seen.add(sname)
        run = str(s.get("run", "")).strip()
        if not run:
            raise DefinitionError(f"{source}: step {sname!r} has no run command")
        kind = str(s.get("kind", "check"))
        if kind not in STEP_KINDS:
            raise DefinitionError(f"{source}: step {sname!r} kind must be one of {STEP_KINDS}")
        timeout = float(s.get("timeout", DEFAULT_STEP_TIMEOUT))
        if timeout <= 0:
            raise DefinitionError(f"{source}: step {sname!r} timeout must be positive")
        steps.append(Step(name=sname, run=run, kind=kind, timeout=timeout,
                          prompt=str(s.get("prompt", ""))))

    agent = _agent(raw.get("agent"), "agent")
    if any(s.kind == "condition" for s in steps) and not (
            agent.prompt or all(s.prompt for s in steps if s.kind == "condition")):
        raise DefinitionError(
            f"{source}: a condition step needs a prompt, on the step or in [agent]")

    return Routine(
        name=name,
        description=str(head.get("description", "")),
        timer=timer,
        call=call,
        steps=tuple(steps),
        agent=agent,
        fixer=_agent(raw["fixer"], "fixer") if "fixer" in raw else None,
    )


def load(path: Path) -> Routine:
    return parse(path.read_text(), source=str(path))

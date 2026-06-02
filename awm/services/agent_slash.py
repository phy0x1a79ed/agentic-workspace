"""Server-side slash-command catalog for agent control.

Slash commands posted to a room with a ``to:`` scope are routed here. If
the command matches a registered server command, the handler runs and a
result message is posted back to the room. Unknown commands fall through
to the scope's claude stdin (via ``agent_instances.send_slash``) so /clear,
/compact, plugin commands, etc. still work.

The catalog is intentionally small and explicit — each entry has a name,
arg signature for the help panel, a one-line description, and an async
handler that takes ``(scope_key, args)`` and returns the result string
that will be posted to the room as a ``system`` message.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from awm.services import agent_instances


@dataclass
class SlashCommand:
    name: str           # leading slash included, e.g. "/restart"
    args: str           # display string for the help panel, e.g. "[mode]"
    description: str
    handler: Callable[[str, list[str]], Awaitable[str]] = field(repr=False)

    def to_help(self) -> dict:
        return {
            "name": self.name,
            "args": self.args,
            "description": self.description,
        }


class SlashParseError(Exception):
    """The command line couldn't be parsed."""


def parse_command_line(line: str) -> tuple[str, list[str]]:
    """Split ``"/mode plan"`` into ``("/mode", ["plan"])``."""
    line = (line or "").strip()
    if not line.startswith("/"):
        raise SlashParseError(f"not a slash command: {line!r}")
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        raise SlashParseError(f"bad quoting: {exc}") from exc
    if not tokens:
        raise SlashParseError("empty command")
    return tokens[0], tokens[1:]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _h_restart(scope_key: str, args: list[str]) -> str:
    mode = args[0] if args else None
    if mode is not None and mode not in agent_instances._VALID_PERMISSION_MODES:
        return (
            f"invalid permission-mode {mode!r}; "
            f"choices: {list(agent_instances._VALID_PERMISSION_MODES)}"
        )
    sess = await agent_instances.respawn_session(
        scope_key, permission_mode=mode,
    )
    return (
        f"restarted {scope_key} (pid={sess.proc.pid}, "
        f"mode={sess.permission_mode}, model={sess.model or 'default'}, "
        f"effort={sess.effort or 'default'})"
    )


async def _h_kill(scope_key: str, args: list[str]) -> str:
    sess = agent_instances._by_scope.get(scope_key)
    if sess is None:
        return f"no active session for {scope_key}"
    await agent_instances.kill_session(sess.id)
    return f"killed {scope_key} (pid={sess.proc.pid})"


async def _h_mode(scope_key: str, args: list[str]) -> str:
    if not args:
        return f"usage: /mode <{'|'.join(agent_instances._VALID_PERMISSION_MODES)}>"
    return await _h_restart(scope_key, args[:1])


async def _h_plan(scope_key: str, args: list[str]) -> str:
    return await _h_restart(scope_key, ["plan"])


async def _h_yolo(scope_key: str, args: list[str]) -> str:
    return await _h_restart(scope_key, ["bypassPermissions"])


async def _h_model(scope_key: str, args: list[str]) -> str:
    if not args:
        return "usage: /model <opus|sonnet|haiku|...>"
    sess = await agent_instances.respawn_session(scope_key, model=args[0])
    return f"respawned {scope_key} on model={sess.model}"


async def _h_effort(scope_key: str, args: list[str]) -> str:
    if not args:
        return f"usage: /effort <{'|'.join(agent_instances._VALID_EFFORTS)}>"
    if args[0] not in agent_instances._VALID_EFFORTS:
        return (
            f"invalid effort {args[0]!r}; "
            f"choices: {list(agent_instances._VALID_EFFORTS)}"
        )
    sess = await agent_instances.respawn_session(scope_key, effort=args[0])
    return f"respawned {scope_key} on effort={sess.effort}"


async def _h_cli(scope_key: str, args: list[str]) -> str:
    supported = sorted(agent_instances._SUPPORTED_CLIS)
    if not args:
        return f"usage: /cli <{'|'.join(supported)}>"
    target = args[0]
    if target not in agent_instances._SUPPORTED_CLIS:
        return (
            f"unsupported agent cli {target!r}; "
            f"choices: {supported}"
        )
    sess = await agent_instances.respawn_session(scope_key, agent_cli=target)
    return f"respawned {scope_key} on cli={sess.agent_cli}"


async def _h_compact(scope_key: str, args: list[str]) -> str:
    # Claude's `/compact` REPL command isn't honored by the headless
    # stream-json pipeline awm uses, so we synthesize the same outcome
    # ourselves: ask the agent to self-summarize, capture the summary
    # from the structured transcript, respawn with the summary as primer.
    # Implementation in agent_instances.compact_session.
    return await agent_instances.compact_session(scope_key)


async def _h_clear(scope_key: str, args: list[str]) -> str:
    sess = agent_instances._by_scope.get(scope_key)
    if sess is None:
        return f"no active session for {scope_key}"
    # Operator-initiated wipe must outrace any pending auto-resume:
    # remove queue rows for this scope before respawn fresh, otherwise
    # the driver might race to bring back the prior --resume id.
    agent_instances.scrub_resume_queue_for_scope(sess.project, sess.scope)
    new = await agent_instances.respawn_session(scope_key, clear_history=True)
    return (
        f"cleared {scope_key} (pid={new.proc.pid}, "
        f"mode={new.permission_mode}, model={new.model or 'default'}); "
        f"conversation context wiped"
    )


async def _h_help(scope_key: str, args: list[str]) -> str:
    lines = ["server slash commands:"]
    for cmd in REGISTRY.values():
        lines.append(f"  {cmd.name} {cmd.args}  — {cmd.description}")
    sess = agent_instances._by_scope.get(scope_key)
    if sess and sess.claude_slash_commands:
        lines.append("")
        lines.append(f"claude commands for {scope_key}:")
        for c in sess.claude_slash_commands:
            lines.append(f"  /{c}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_COMMANDS: list[SlashCommand] = [
    SlashCommand("/restart", "[mode]", "Kill and respawn the agent, preserving conversation via --resume. Optional permission-mode.", _h_restart),
    SlashCommand("/kill", "", "SIGKILL the agent.", _h_kill),
    SlashCommand("/mode", "<mode>", "Respawn with permission-mode. Choices: default, plan, bypassPermissions, acceptEdits.", _h_mode),
    SlashCommand("/plan", "", "Shortcut for /mode plan.", _h_plan),
    SlashCommand("/yolo", "", "Shortcut for /mode bypassPermissions.", _h_yolo),
    SlashCommand("/model", "<name>", "Respawn with --model. e.g. opus, sonnet, haiku, or full id.", _h_model),
    SlashCommand("/effort", "<level>", "Respawn with --effort. Choices: low, medium, high, xhigh, max.", _h_effort),
    SlashCommand("/cli", "<name>", "Respawn under a different agent CLI (e.g. claude). Listed in /cli with no args.", _h_cli),
    SlashCommand("/compact", "", "Not supported in headless mode — claude /compact is REPL-only.", _h_compact),
    SlashCommand("/clear", "", "Wipe conversation context: respawn without --resume.", _h_clear),
    SlashCommand("/help", "", "Echo this catalog plus the scope's claude commands.", _h_help),
]

REGISTRY: dict[str, SlashCommand] = {c.name: c for c in _COMMANDS}


def server_catalog() -> list[dict]:
    return [c.to_help() for c in _COMMANDS]


def lookup(name: str) -> Optional[SlashCommand]:
    return REGISTRY.get(name)


async def dispatch(scope_key: str, line: str) -> tuple[bool, str]:
    """Parse and dispatch ``line`` for ``scope_key``.

    Returns ``(handled, message)``:
      - ``handled=True``: a registered server command ran; message is its
        result string (post as ``system`` to the room).
      - ``handled=False``: the command isn't registered server-side; the
        caller should forward the raw line to the scope's claude stdin
        via ``agent_instances.send_slash``. ``message`` is empty.

    Raises ``SlashParseError`` if ``line`` isn't a valid slash form.
    """
    name, args = parse_command_line(line)
    cmd = REGISTRY.get(name)
    if cmd is None:
        return False, ""
    return True, await cmd.handler(scope_key, args)

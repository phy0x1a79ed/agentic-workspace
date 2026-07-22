"""Register the Claude Code hooks that feed claude-view its agent state.

claude-view can do this itself, and by default it does — but doing it its way
costs more than it gives. ``register_hooks()`` upstream also injects a
*statusline*, and the statusline is a single slot in ``settings.json`` rather
than an array: a dev instance and a prod instance would overwrite each other's,
and the wrapper script it writes lands in ``~/.claude-view/``, outside the data
dir that the rest of our containment rests on. The same flag that disables the
statusline (``CLAUDE_VIEW_SKIP_HOOKS=1``) also disables the hooks, and it is not
separable — so we keep the flag set and do the half we want ourselves.

That half is not optional decoration. ``routes/hooks/resolve_state.rs`` calls
itself "the SOLE authority for agent state", and it means it: with no hooks
posting to ``/api/live/hook`` every session resolves to ``state: "unknown"``,
which the UI buckets as "Needs You". The Live Monitor then reports that every
agent is waiting on you while they are all working — which is precisely the
question the fleet dashboard exists to answer.

**The one thing that must never go wrong here.** Claude Code has two kinds of
hooks. *Observation* hooks fire alongside the action and their stdout is
discarded. *Replacement* hooks REPLACE the action, and Claude Code parses their
stdout as the result. ``WorktreeCreate`` is a replacement hook: registering it
as an observer makes it print ``{"ok":true}`` where a worktree path belongs, and
every ``isolation: "worktree"`` subagent call in the fleet fails — silently, and
nowhere near this file. So the taxonomy below is transcribed from upstream's
``ALL_HOOKS`` with the kinds intact, ``install()`` asserts the two sets are
disjoint before it writes a byte, and ``WorktreeCreate`` is never written.

The sentinel is deliberately upstream's own string. If someone ever drops
``CLAUDE_VIEW_SKIP_HOOKS``, claude-view's cleanup pass recognises and reclaims
our entries instead of stacking a second copy beside them, and ours does the
same to its. One namespace, no orphans, whichever side is running.

**Exactly one instance on a host may own this file.** The hooks name a single
port, so agent state can only ever flow to one instance, and ``install`` claims
that slot by clearing every claude-view entry rather than just its own. Which
instance gets the claim is therefore not a preference — it is decided in
``owns_fleet_settings``, structurally and fail-safe, so that a dev instance
started alongside prod writes to a scratch file instead of silently taking
prod's hooks away and never giving them back.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("awm.claude_view.hooks")

#: Upstream's marker, embedded in every command string we write. Shared on
#: purpose — see the module docstring.
SENTINEL = "# claude-view-hook"

#: The fleet-wide file every Claude Code session on this host reads.
FLEET_SETTINGS = Path.home() / ".claude" / "settings.json"

#: The gateway the fleet's singleton instance registers with (prod systemd).
PROD_HUB_PORT = int(os.environ.get("AWM_PROD_PORT", "7819"))

_SERVICE_DIR = Path(__file__).resolve().parents[2]


def owns_fleet_settings() -> bool:
    """True only for the instance the fleet's agents should be reporting to.

    Hooks are a **singleton** resource, and ``install`` claims the slot
    outright — it strips every claude-view entry regardless of port, because a
    stale instance's hooks curl into a dead port on every tool call of every
    agent. That is correct for one instance and destructive with two: a dev
    instance starting up would strip prod's entries, and ``remove``, scoped to
    its own port, would not put them back on the way out. Prod would then run
    with no hooks at all — which does not surface as an error anywhere, because
    every session simply resolves to ``unknown`` and the Live Monitor reports
    the entire fleet as "Needs You" while it works.

    So ownership is decided structurally rather than by convention, and it
    fails safe: the claim belongs to the instance registered with the
    production gateway, and anything else — a dev sandbox on :7861, a
    standalone run with no hub at all — writes to a scratch file instead.
    Forgetting to configure a dev instance costs that instance its agent state.
    It cannot cost prod's.

    ``CLAUDE_VIEW_SETTINGS`` still overrides the result wholesale, which is how
    a deliberate test writes somewhere specific (including at the real file,
    with prod stopped).
    """
    try:
        return urlsplit(os.environ.get("AWM_HUB_URL") or "").port == PROD_HUB_PORT
    except ValueError:  # malformed AWM_HUB_URL — treat as "not prod"
        return False


def _default_settings_path() -> Path:
    if owns_fleet_settings():
        return FLEET_SETTINGS
    state = Path(os.environ.get("CLAUDE_VIEW_STATE_DIR") or (_SERVICE_DIR / "state"))
    return state / "settings.dev.json"


SETTINGS_PATH = Path(
    os.environ.get("CLAUDE_VIEW_SETTINGS") or _default_settings_path()
)

#: Transcribed from ``crates/server/src/live/hook_registrar.rs`` (taxonomy
#: snapshot 2026-04-16, 26 events). Every event Claude Code fires *alongside*
#: the action, so observing it cannot change what the action does.
OBSERVATION_EVENTS: tuple[str, ...] = (
    # session lifecycle
    "SessionStart", "SessionEnd",
    # user input
    "UserPromptSubmit",
    # tools
    "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "PermissionRequest", "PermissionDenied",
    # turn end
    "Stop", "StopFailure",
    "Notification",
    # sub-entities
    "SubagentStart", "SubagentStop", "TeammateIdle",
    "TaskCreated", "TaskCompleted",
    # context management
    "PreCompact", "PostCompact",
    # configuration / environment
    "InstructionsLoaded", "ConfigChange", "CwdChanged", "FileChanged",
    # worktree — note WorktreeCreate's absence, and see REPLACEMENT_EVENTS
    "WorktreeRemove",
    # MCP elicitation
    "Elicitation", "ElicitationResult",
)

#: Hooks whose stdout Claude Code consumes as the result of the action. Writing
#: one of these as an observer breaks the feature it backs. Never registered.
REPLACEMENT_EVENTS: tuple[str, ...] = ("WorktreeCreate",)

#: Upstream fires this one synchronously; everything else is async so the hook
#: never sits in the agent's critical path.
SYNC_EVENTS: frozenset[str] = frozenset({"SessionStart"})

#: The full taxonomy is 26 events. If Claude Code adds one, upstream's build
#: breaks on its own canary and ours should be updated in the same pass.
EXPECTED_TAXONOMY_LEN = 26


def _command(port: int) -> str:
    """Upstream's exact handler command, so both sides read the same entries."""
    return (
        f"curl -s -X POST http://localhost:{port}/api/live/hook "
        "-H 'Content-Type: application/json' "
        "-H 'X-Claude-PID: '$PPID "
        f"--data-binary @- 2>/dev/null || true {SENTINEL}"
    )


def _matcher_group(port: int, event: str) -> dict:
    """One matcher group. No ``matcher`` key means "every occurrence"."""
    handler: dict = {
        "type": "command",
        "command": _command(port),
        "statusMessage": "Live Monitor",
    }
    if event not in SYNC_EVENTS:
        handler["async"] = True
    return {"hooks": [handler]}


def _is_ours(group: object, port: int | None = None) -> bool:
    """True if this matcher group was written by claude-view (or by us).

    ``port=None`` matches any instance's entries; a port matches only that
    instance's. The asymmetry is deliberate — see ``install`` and ``remove``.
    """
    if not isinstance(group, dict):
        return False
    for handler in group.get("hooks") or []:
        if not isinstance(handler, dict):
            continue
        cmd = handler.get("command")
        if not isinstance(cmd, str) or SENTINEL not in cmd:
            continue
        if port is None or f"localhost:{port}/api/live/hook" in cmd:
            return True
    return False


def _strip(hooks: dict, port: int | None) -> int:
    """Remove our matcher groups in place; drop arrays left empty. Returns count.

    User-authored entries are matched by neither branch and are never touched.
    """
    removed = 0
    for event in list(hooks.keys()):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept = [g for g in groups if not _is_ours(g, port)]
        removed += len(groups) - len(kept)
        if kept:
            hooks[event] = kept
        else:
            # Leave no `"Event": []` behind — upstream drops these too, and an
            # empty array is indistinguishable from a user disabling the event.
            del hooks[event]
    return removed


def _load() -> dict:
    """Read settings.json, treating anything unreadable as empty.

    Never raises. A malformed settings.json is the user's problem to fix, but
    it must not stop the service from starting.
    """
    if not SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(SETTINGS_PATH.read_text())
    except Exception as exc:  # noqa: BLE001
        log.warning("settings.json unreadable (%s); not registering hooks", exc)
        raise
    return data if isinstance(data, dict) else {}


def _save(settings: dict) -> None:
    """Serialise, validate, then replace atomically.

    Claude Code skips a settings file with a JSON error *entirely* — not just
    the offending key — so a half-written file would silently disable every
    setting the user has. Render and re-parse before anything touches the real
    path, and swap it in with ``os.replace``.
    """
    content = json.dumps(settings, indent=2) + "\n"
    json.loads(content)  # cheap proof we are about to write valid JSON
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=SETTINGS_PATH.parent, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, SETTINGS_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def install(port: int) -> dict:
    """Point every observation hook at ``port``. Idempotent. Never raises.

    Clears *all* claude-view entries first, not just our own port's: a stale
    instance's hooks curl into a dead port on every single event, and only one
    instance can usefully own the state stream anyway. Last writer wins, and
    the win is total.
    """
    overlap = set(OBSERVATION_EVENTS) & set(REPLACEMENT_EVENTS)
    if overlap:
        # Refuse rather than write. See the module docstring: the failure this
        # guards is silent, remote, and fleet-wide.
        log.error("hook taxonomy corrupt — %s is both observation and "
                  "replacement; refusing to write settings.json", overlap)
        return {"installed": False, "error": f"taxonomy overlap: {sorted(overlap)}"}
    if len(OBSERVATION_EVENTS) + len(REPLACEMENT_EVENTS) != EXPECTED_TAXONOMY_LEN:
        log.warning("hook taxonomy has %d events, expected %d — Claude Code may "
                    "have added events we are not observing",
                    len(OBSERVATION_EVENTS) + len(REPLACEMENT_EVENTS),
                    EXPECTED_TAXONOMY_LEN)

    try:
        settings = _load()
        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        _strip(hooks, port=None)
        for event in OBSERVATION_EVENTS:
            hooks.setdefault(event, []).append(_matcher_group(port, event))
        settings["hooks"] = hooks
        _save(settings)
    except Exception as exc:  # noqa: BLE001 — a settings problem is not fatal
        log.exception("claude-view: hook registration failed")
        return {"installed": False, "error": f"{type(exc).__name__}: {exc}"}

    log.info("claude-view: registered %d observation hooks → port %d",
             len(OBSERVATION_EVENTS), port)
    return {"installed": True, "events": len(OBSERVATION_EVENTS), "port": port}


def remove(port: int | None = None) -> dict:
    """Strip our hook entries. Never raises.

    Scoped to ``port`` by default so that shutting down one instance does not
    tear out the hooks another instance has since installed — the reverse of
    ``install``, which deliberately claims the slot outright.
    """
    try:
        settings = _load()
        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            return {"removed": 0}
        n = _strip(hooks, port=port)
        if n:
            if hooks:
                settings["hooks"] = hooks
            else:
                settings.pop("hooks", None)
            _save(settings)
    except Exception as exc:  # noqa: BLE001
        log.exception("claude-view: hook removal failed")
        return {"removed": 0, "error": f"{type(exc).__name__}: {exc}"}
    log.info("claude-view: removed %d hook entries", n)
    return {"removed": n}


def status(port: int | None = None) -> dict:
    """What is actually in settings.json right now. Never raises.

    ``fleet_wide`` is the one to read first: false means this instance is
    writing a scratch file that no Claude Code session loads, so every session
    it shows will read "Needs You" no matter what the agents are doing. That is
    the correct and deliberate state for a non-prod instance — see
    ``owns_fleet_settings``.
    """
    out: dict = {
        "settings": str(SETTINGS_PATH),
        "fleet_wide": SETTINGS_PATH == FLEET_SETTINGS,
        "ours": 0,
        "events": [],
    }
    try:
        hooks = _load().get("hooks")
        if not isinstance(hooks, dict):
            return out
        for event, groups in hooks.items():
            if not isinstance(groups, list):
                continue
            mine = [g for g in groups if _is_ours(g, port)]
            if mine:
                out["ours"] += len(mine)
                out["events"].append(event)
        out["events"].sort()
        out["complete"] = set(out["events"]) == set(OBSERVATION_EVENTS)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out

"""Game-bot runtime — recurring, event-woken play sessions (feat-gamebot).

A **game bot** is the third session shape next to placements and (retired)
conversational scopes: a bounded play session on a persistent, reused workspace
unit, spawned by a ``schedule.tick`` / ``agent.wake`` event (or an explicit
``game_spawn``), that plays through the realm's MCP verbs and ends itself by
saving the world and calling ``park``. It deliberately bypasses the whole
orchestrator/DAG stack — there is no task, no contract, no deliverable; the
world state lives in the realm's saves and the bot's continuity lives in its
unit's ``journal.md``.

Identity + lifecycle:

- One unit per game, slug ``game-<game>`` — the slug is the agent identity
  (``AWM_AS``) exactly as for placements, and the between-ticks "park" state is
  simply *no live session on the slug*. ``spawn_for_game`` dedupes on
  ``get_session_by_scope``, so a tick landing mid-session is a no-op.
- Each tick runs a **fresh** session (no harness resume) on the SAME unit: the
  brief instructs the bot to read + update ``journal.md`` as its cross-session
  memory, and the realm's named saves carry the world.
- The turn driver (:func:`on_turn_boundary`) mirrors the placement supervisor:
  hard per-session turn budget from the game's registry entry, continuation
  nudges, an escalating save-and-park warning, force-stop at zero.

The per-game registry is a committed ``games/<game>.toml`` in this service's
folder — realm, goal prompt, harness/model, turn budget. The default driver is
**opencode on the free Zen DeepSeek** (model ``None`` → the opencode backend's
own default): cheap enough to run on a cron. NOTE the standing caveat: opencode
ignores ``allowed_tools``, so the tool profile below is only enforced when a
game pins ``harness = "claude"`` — on opencode it is a guardrail-not-boundary,
consistent with the loopback bus precedent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import awm.gatewayclient as gatewayclient
from awm.agents import agent_instances as ai

log = logging.getLogger("awm.agents.gamebot")


class GamebotError(Exception):
    """A game-bot verb was called badly (unknown game, no session, wrong mode)."""


class UnknownGameError(GamebotError):
    """No committed registry entry (``games/<game>.toml``) for the game."""


# ---------------------------------------------------------------------------
# Tool profile — enforced via --allowedTools on claude only (see module doc)
# ---------------------------------------------------------------------------

_RLM_DOMAIN = "mcp__awm__rlm"
_AGENT_DOMAIN = "mcp__awm__agent"

# Minimal filesystem (journal.md upkeep) + the realm domain + the agent domain
# (park rides it). No Bash, no web: the bot acts on the world through the realm.
GAMEBOT_TOOLS = [
    "Read", "Write", "Edit", "Grep", "Glob", "TodoWrite",
    _RLM_DOMAIN, _AGENT_DOMAIN,
]

DEFAULT_TURN_BUDGET = 40
WARN_REMAINING = 5


# ---------------------------------------------------------------------------
# Per-game registry (committed games/<game>.toml in the service folder)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GameSpec:
    game: str
    realm: str
    goal: str
    harness: str = "opencode"
    model: str | None = None
    effort: str | None = None
    turn_budget: int = DEFAULT_TURN_BUDGET


def games_dir() -> Path:
    """The committed per-game registry dir (``<service root>/games``).

    ``AWM_GAMES_DIR`` overrides it (tests point this at a temp tree)."""
    if env := os.environ.get("AWM_GAMES_DIR"):
        return Path(env).resolve()
    # gamebot.py → awm/agents/ → awm/ → <service root>
    return Path(__file__).resolve().parents[2] / "games"


def load_game(game: str) -> GameSpec:
    """Load one game's registry entry, or raise :class:`UnknownGameError`."""
    path = games_dir() / f"{game}.toml"
    if not path.is_file():
        raise UnknownGameError(
            f"no registry entry for game {game!r} (expected {path})")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GamebotError(f"bad registry entry for {game!r}: {exc}") from exc
    goal = str(data.get("goal") or "").strip()
    if not goal:
        raise GamebotError(f"registry entry for {game!r} has no goal")
    return GameSpec(
        game=game,
        realm=str(data.get("realm") or "").strip(),
        goal=goal,
        harness=str(data.get("harness") or "opencode"),
        model=data.get("model") or None,
        effort=data.get("effort") or None,
        turn_budget=int(data.get("turn_budget") or DEFAULT_TURN_BUDGET),
    )


def list_games() -> list[str]:
    root = games_dir()
    if not root.is_dir():
        return []
    return sorted(p.stem for p in root.glob("*.toml"))


def unit_slug_for(game: str) -> str:
    return f"game-{game}"


# ---------------------------------------------------------------------------
# Brief + kickoff
# ---------------------------------------------------------------------------

def render_game_brief(spec: GameSpec) -> str:
    """The unit's CLAUDE.md — persistent across sessions (the unit is reused)."""
    return (
        f"# Game bot: {spec.game}\n\n"
        f"You are the recurring **{spec.game}** game bot. Each session is one "
        "bounded play sitting; the world persists in the realm between "
        "sessions, and `./journal.md` is your only memory across sessions.\n\n"
        f"## Goal\n\n{spec.goal}\n\n"
        "## How to play\n\n"
        f"All game verbs ride the single `rlm` tool: call it as "
        f"`rlm(verb=\"<name>\", args={{...}})` — `rlm(verb=\"describe\")` "
        f"lists every verb with its parameters (yours are the "
        f"`{spec.realm or 'rlm-*'}` family, e.g. verb `factorio_observe`). "
        "Acquire/reuse the realm session first if the world isn't up, then "
        "observe before you act.\n\n"
        "## Every session, in order\n\n"
        "1. Read `./journal.md` (if present) — where you left off, what to do "
        "next.\n"
        "2. Play toward the goal.\n"
        "3. Before stopping: SAVE the world through the realm's save verb, "
        "update `./journal.md` (what you did, world state, next steps), then "
        "call `agent(verb=\"park\")` to end your session cleanly.\n\n"
        f"_You are unattended on a hard {spec.turn_budget}-turn budget; the "
        "supervisor warns you near the end — park before it expires._\n"
    )


def _kickoff_text(spec: GameSpec) -> str:
    return (
        f"You are the {spec.game} game bot, woken for one play session. Read "
        "`./CLAUDE.md` for your goal and how to play, then `./journal.md` for "
        "where you left off. Play, then save + update the journal + "
        "`agent(verb=\"park\")` when done. Begin now."
    )


# ---------------------------------------------------------------------------
# Spawn / park
# ---------------------------------------------------------------------------

async def spawn_for_game(game: str, *, source: str = "manual") -> dict:
    """Spawn one bounded play session for ``game`` (dedupes on the live slug).

    The dedupe IS the park semantics: between sessions the slug has no live
    session; a tick during a session returns ``{"spawned": False}``."""
    spec = load_game(game)
    slug = unit_slug_for(game)

    if ai.get_session_by_scope(slug) is not None:
        log.info("gamebot: %s already playing (source=%s) — skipping", slug, source)
        return {"spawned": False, "scope": slug, "reason": "already playing"}

    # Provision (or re-activate) the persistent unit. Idempotent on the slug —
    # journal.md and any prior state survive; only the brief is (re)written.
    res = await gatewayclient.call("workspace", "workspace_create", {
        "unit_slug": slug,
        "context_md": render_game_brief(spec),
        "prereadings": [],
        "repos": [],
    })
    workspace_path = (res or {}).get("path") or ""
    if not workspace_path:
        raise GamebotError(f"workspace_create returned no path for {slug}")

    # Fresh session every sitting (journal.md is the memory, not the harness
    # transcript). mode="gamebot" routes the turn driver in _reader_loop and
    # keeps every placement path (token-less) inert for this row.
    session = await ai.create_session(
        scope=slug,
        agent_cli=spec.harness,
        permission_mode="bypassPermissions",
        mode="gamebot",
        workdir=workspace_path,
        allowed_tools=list(GAMEBOT_TOOLS),
        model=spec.model,
        effort=spec.effort,
        fresh=True,
    )
    session.turn_budget = spec.turn_budget
    ai._get_dao().merge_instance_data(session.id, {
        "gamebot": {"game": game, "source": source},
    })
    ai.enqueue_input(session, "gamebot", _kickoff_text(spec))
    log.info("gamebot: spawned %s (session %s, harness=%s, source=%s)",
             slug, session.id, spec.harness, source)
    return {"spawned": True, "scope": slug, "session_id": session.id,
            "harness": spec.harness, "turn_budget": spec.turn_budget}


async def park(args: dict, as_: str | None = None) -> dict:
    """Bot-callable: end this play session cleanly (the bot's own stop).

    Resolves the session from the CALLER IDENTITY (``AWM_AS`` → ``X-Awm-As`` →
    ``as_`` — the unit slug), exactly like the placement B-ops: the model never
    passes an id. An operator can also pass an explicit ``scope``."""
    scope = args.get("scope") or as_
    if not scope:
        raise GamebotError("park: missing caller identity (no AWM_AS) and no scope")
    session = ai.get_session_by_scope(scope)
    if session is None:
        return {"parked": False, "scope": scope, "reason": "no live session"}
    if getattr(session, "mode", None) != "gamebot":
        raise GamebotError(f"park: {scope} is not a game-bot session")
    log.info("gamebot: parking %s (session %s)", scope, session.id)
    await ai.stop_session(session.id)
    return {"parked": True, "scope": scope, "session_id": session.id}


# ---------------------------------------------------------------------------
# Turn driver (dispatched from _reader_loop on `result` for mode="gamebot")
# ---------------------------------------------------------------------------

def _continuation_prompt(game: str, remaining: int) -> str:
    if remaining <= WARN_REMAINING:
        return (
            f"[supervisor] {remaining} turn(s) left in this sitting. Wrap up "
            "NOW: save the world via the realm's save verb, update "
            "`./journal.md`, then call `agent(verb=\"park\")`. Do not start "
            "new work."
        )
    return (
        f"[supervisor] Continue playing {game} ({remaining} turns left this "
        "sitting). When you reach a good stopping point: save, update "
        "`./journal.md`, then `agent(verb=\"park\")`."
    )


async def on_turn_boundary(session) -> None:
    """Drive one game bot at a turn boundary: budget countdown → nudge →
    force-park at zero. A parked/ended session no-ops (the stop raced us)."""
    if ai.get_session_by_scope(session.scope) is not session:
        return  # already parked / exited
    row = ai._get_dao().get_instance(session.id)
    if row is None or row.get("ended_at") is not None:
        return
    import json as _json
    try:
        data = _json.loads(row.get("data") or "{}")
    except (TypeError, ValueError):
        data = {}
    game = (data.get("gamebot") or {}).get("game") or session.scope

    session.turn_budget -= 1
    remaining = session.turn_budget
    if remaining <= 0:
        log.info("gamebot: %s turn budget exhausted — force-parking", session.scope)
        try:
            await ai.stop_session(session.id)
        except Exception:  # noqa: BLE001
            log.warning("gamebot: force-park failed for %s", session.scope,
                        exc_info=True)
        return
    ai.enqueue_input(session, "supervisor", _continuation_prompt(game, remaining))


# ---------------------------------------------------------------------------
# Event consumers (schedule.tick + agent.wake) — the autonomous wake path
# ---------------------------------------------------------------------------

_WAKE_TOPICS = [
    ("events", "schedule.tick"),
    ("events", "agent.wake"),
]


async def _topic_listener(service: str, topic: str) -> None:
    """One never-die consumer loop over a gateway emitter (the 2fa envelope).

    ``subscribe`` yields one socket's worth of events then returns when it
    closes — reconnect/backoff is ours. Best-effort: while the emitting service
    is absent the connect just fails and we retry, so this is inert until it
    shows up. An event for an unregistered game logs and drops."""
    log.info("gamebot: subscribing to %s/%s", service, topic)
    backoff = 2.0
    while True:
        try:
            async for ev in gatewayclient.subscribe(service, topic):
                backoff = 2.0  # connected and receiving
                if not isinstance(ev, dict):
                    continue
                game = str(ev.get("game") or "").strip()
                if not game:
                    continue
                try:
                    await spawn_for_game(game, source=topic)
                except UnknownGameError:
                    log.info("gamebot: %s for unregistered game %r — dropped",
                             topic, game)
                except Exception:  # noqa: BLE001
                    log.warning("gamebot: spawn for %r (via %s) failed",
                                game, topic, exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — never let the loop die
            log.debug("gamebot: %s/%s subscription dropped (retrying): %s",
                      service, topic, exc)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 1.5, 30.0)


async def run_listeners() -> None:
    """The agents service's gamebot wake fabric — gathered next to the adapter
    loop in ``hub_adapter.main`` (the events-service Scheduler pattern)."""
    await asyncio.gather(*(
        _topic_listener(svc, topic) for svc, topic in _WAKE_TOPICS
    ))

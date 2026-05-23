"""Higher-level glue between rooms, agent instances, and federation.

This is the small "what do I do when a user creates a room with scopes"
orchestration that sits above rooms.py (data layer) and agent_instances.py
(agent runtime). It exists so the rooms HTTP router and CLI don't have
to duplicate the spawn-if-needed dance.
"""

from __future__ import annotations

import asyncio

from awm.services import rooms as rooms_svc
from awm.services import agent_instances


def _split_scope(s: str) -> tuple[str, str, str | None]:
    """Return (project, scope, peer_or_None) for an identifier like
    ``project/scope`` or ``project/scope@peer``."""
    base = s
    peer: str | None = None
    if "@" in base:
        base, peer = base.rsplit("@", 1)
    if "/" not in base:
        raise ValueError(
            f"scope identifier must be 'project/scope[@peer]', got {s!r}"
        )
    project, scope = base.split("/", 1)
    return project, scope, peer


async def _ensure_local_session(scope_key: str) -> None:
    """Spawn a AgentInstance for ``scope_key`` if one isn't running.
    No-op if it is."""
    if agent_instances._by_scope.get(scope_key) is not None:
        return
    project, scope = scope_key.split("/", 1)
    try:
        await agent_instances.create_session(project=project, scope=scope)
    except agent_instances.ScopeBusyError:
        return  # raced with another caller; that's fine
    except FileNotFoundError:
        raise


async def create_room_with_scopes(*, topic: str | None,
                                  scopes: list[str],
                                  prompts: dict[str, str],
                                  opener: str,
                                  close_on_exit: bool) -> rooms_svc.Room:
    """Create a room, spawn any local agents that need spawning, and post
    the per-scope opener prompts.

    For remote scopes (``project/scope@peer``), the participant row is
    written here; the actual session spawn is forwarded via M3 federation
    when the first post lands.
    """
    # Validate up-front.
    parsed = [(_split_scope(s), s) for s in scopes]
    for (project, scope, peer), raw in parsed:
        if peer is None:
            from awm.config import PROJECTS_DIR
            ws = PROJECTS_DIR / project / scope
            if not ws.exists():
                raise FileNotFoundError(
                    f"Scope workspace not found at {ws} (for {raw})"
                )

    room = rooms_svc.create_room(
        topic=topic, scopes=scopes, opener=opener,
        close_on_exit=close_on_exit,
    )

    local_sessions: list[agent_instances.AgentInstance] = []
    for (project, scope, peer), raw in parsed:
        if peer is None:
            scope_key = f"{project}/{scope}"
            await _ensure_local_session(scope_key)
            sess = agent_instances._by_scope.get(scope_key)
            if sess is not None:
                local_sessions.append(sess)
        else:
            # M3: federation.forward_agent_input will spawn on demand.
            pass

        prompt = prompts.get(raw) or prompts.get(f"{project}/{scope}")
        if prompt:
            rooms_svc.post(room.id, author=opener, body=prompt, kind="text")

    if close_on_exit:
        for sess in local_sessions:
            asyncio.create_task(_close_stdin_after_drain(sess))

    return room


async def _close_stdin_after_drain(session: agent_instances.AgentInstance) -> None:
    """For close_on_exit rooms: let the input pump drain the prompt(s), then
    close stdin so claude finishes the response and exits naturally."""
    # Wait for queue to empty.
    for _ in range(200):  # up to 10s
        if session.input_queue.empty():
            break
        await asyncio.sleep(0.05)
    # Let the pump's last write/drain finish.
    await asyncio.sleep(0.5)
    proc = session.proc
    if proc is None or proc.stdin is None:
        return
    if proc.stdin.is_closing():
        return
    try:
        proc.stdin.close()
    except Exception:
        pass


async def invite_scope_to_room(room_id: str, scope_identifier: str, *,
                               prompt: str | None,
                               opener: str) -> rooms_svc.Participant:
    """Add a scope (local or remote) as a participant. Spawn the local
    AgentInstance if needed. Post an optional initial prompt."""
    project, scope, peer = _split_scope(scope_identifier)
    if peer is None:
        await _ensure_local_session(f"{project}/{scope}")
    # Even if the session is busy, we still want the room participant
    # link (the existing session will just receive frames from this room
    # too).
    participant = rooms_svc.add_participant(room_id, "scope", scope_identifier)
    if prompt:
        rooms_svc.post(room_id, author=opener, body=prompt, kind="text")
    return participant

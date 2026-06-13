"""Higher-level glue: room+scope orchestration, modular edition.

Rooms operations are cross-service — they go via gatewayclient to the scopes
service (``room_create`` / ``room_invite``), which the scopes service exposes
in its ready manifest. Local agent sessions are spawned in this process.
"""
from __future__ import annotations

import asyncio

from awm.agents import agent_instances
import awm.gatewayclient as gatewayclient


def _split_scope(s: str) -> tuple[str, str, str | None]:
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
    if agent_instances._by_scope.get(scope_key) is not None:
        return
    project, scope = scope_key.split("/", 1)
    try:
        await agent_instances.create_session(
            project=project, scope=scope,
            permission_mode="bypassPermissions",
        )
    except agent_instances.ScopeBusyError:
        return
    except FileNotFoundError:
        raise


async def create_room_with_scopes(*, topic: str | None,
                                  scopes: list[str],
                                  prompts: dict[str, str],
                                  opener: str,
                                  close_on_exit: bool):
    """Create a room (via scopes RPC) and spawn local agents.

    Creates the room in the scopes service, then spawns a local session for
    each non-peer scope. Returns the new room id.
    """
    parsed = [(_split_scope(s), s) for s in scopes]
    for (project, scope, peer), raw in parsed:
        if peer is None:
            from awm.config import PROJECTS_DIR
            ws = PROJECTS_DIR / project / scope
            if not ws.exists():
                raise FileNotFoundError(
                    f"Scope workspace not found at {ws} (for {raw})"
                )

    room_resp = await gatewayclient.call('scopes', 'room_create', {
        'topic': topic, 'scopes': scopes,
        'opener': opener, 'close_on_exit': close_on_exit,
    })
    room_id = room_resp.get('id') if room_resp else None

    local_sessions: list[agent_instances.AgentInstance] = []
    for (project, scope, peer), raw in parsed:
        if peer is None:
            scope_key = f"{project}/{scope}"
            await _ensure_local_session(scope_key)
            sess = agent_instances._by_scope.get(scope_key)
            if sess is not None:
                local_sessions.append(sess)

    if close_on_exit:
        for sess in local_sessions:
            asyncio.create_task(_close_stdin_after_drain(sess))

    return room_id


async def _close_stdin_after_drain(session: agent_instances.AgentInstance) -> None:
    for _ in range(200):
        if session.input_queue.empty():
            break
        await asyncio.sleep(0.05)
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
                               opener: str):
    """Add a scope to a room (via scopes RPC) and spawn local agent."""
    project, scope, peer = _split_scope(scope_identifier)
    if peer is None:
        await _ensure_local_session(f"{project}/{scope}")
    result = await gatewayclient.call('scopes', 'room_invite', {
        'room_id': room_id, 'scope': f"{project}/{scope}",
    })
    return result.get('participant') if result else None

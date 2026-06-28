"""Higher-level glue: multi-scope conversation orchestration (modular edition).

A scope IS the channel. To put several scopes in conversation, spawn a local
agent session for each and subscribe the guests to the lead scope's channel via
the scopes service (``scope_subscribe``). Channel posts are cross-service
(``scope_post``); local agent sessions are spawned in this process.
"""
from __future__ import annotations

import asyncio

from awm.agents import agent_instances
import awm.gatewayclient as gatewayclient


def _split_scope(s: str) -> tuple[str, str]:
    """Parse a 'project/scope' identifier."""
    if "/" not in s:
        raise ValueError(f"scope identifier must be 'project/scope', got {s!r}")
    project, scope = s.split("/", 1)
    return project, scope


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


async def start_conversation(*, scopes: list[str],
                             opener: str = "user:operator") -> str | None:
    """Spawn a local agent for each scope and subscribe the guests to the lead
    scope's channel. Returns the lead scope key ('project/scope').

    The first entry is the lead (its channel is the shared conversation); the
    rest subscribe to it.
    """
    parsed = [_split_scope(s) for s in scopes]
    from awm.config import PROJECTS_DIR
    for project, scope in parsed:
        ws = PROJECTS_DIR / project / scope
        if not ws.exists():
            raise FileNotFoundError(f"Scope workspace not found at {ws}")

    if not parsed:
        return None
    lead_project, lead_scope = parsed[0]
    lead_key = f"{lead_project}/{lead_scope}"

    for i, (project, scope) in enumerate(parsed):
        scope_key = f"{project}/{scope}"
        await _ensure_local_session(scope_key)
        if i > 0:
            # Subscribe each guest scope to the lead's channel.
            try:
                await gatewayclient.call('scopes', 'scope_subscribe', {
                    'project': lead_project, 'scope': lead_scope,
                    'guest': scope_key,
                })
            except Exception:  # noqa: BLE001
                pass

    return lead_key


async def invite_scope(lead_key: str, scope_identifier: str) -> dict | None:
    """Spawn a local agent for ``scope_identifier`` and subscribe it to the
    lead scope's channel."""
    lead_project, lead_scope = _split_scope(lead_key)
    project, scope = _split_scope(scope_identifier)
    await _ensure_local_session(f"{project}/{scope}")
    return await gatewayclient.call('scopes', 'scope_subscribe', {
        'project': lead_project, 'scope': lead_scope,
        'guest': f"{project}/{scope}",
    })

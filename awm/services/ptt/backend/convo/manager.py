"""Process-wide owner of the shared LLM backend + convo-session factory.

One warm :class:`OpencodeAgent` is shared across every user's convo session —
the server is stateless per request (fresh session per cut), so sharing the
warm process is safe and avoids N processes. ``ConvoManager`` hands out a fresh
:class:`ConvoSession` per continuous PTT session and tears the agent down on
service shutdown.
"""

from __future__ import annotations

import logging
from typing import Optional

from backend.agent import OpencodeAgent

from .session import ConvoSession

log = logging.getLogger("ptt.convo.manager")


class ConvoManager:
    def __init__(self) -> None:
        self._agent = OpencodeAgent()

    @property
    def agent(self) -> OpencodeAgent:
        """The shared warm LLM backend. Exposed so other in-process consumers
        (e.g. the mock chatbot test harness) can reuse the one server rather
        than spawning a second ``opencode serve``."""
        return self._agent

    async def ensure_started(self) -> None:
        """Warm the opencode server ahead of the first cut (best-effort), so
        the first utterance doesn't pay process-startup latency."""
        try:
            await self._agent.start()
        except Exception:  # noqa: BLE001 — first cut will retry via _ensure_running
            log.warning("convo agent warm-start failed; will retry on first cut",
                        exc_info=True)

    def new_session(self) -> ConvoSession:
        return ConvoSession(self._agent)

    async def shutdown(self) -> None:
        await self._agent.stop()


_manager: Optional[ConvoManager] = None


def get_convo_manager() -> ConvoManager:
    global _manager
    if _manager is None:
        _manager = ConvoManager()
    return _manager

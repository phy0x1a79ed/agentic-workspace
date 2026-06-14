"""Process-wide convo-session factory over the shared cleanup seam.

Each silence-cut runs an ``awm.agentcore`` opencode **one-shot** (open → send →
drain → close per call), so there is no long-lived server to own and share —
the heavy ``OpencodeAgent``/warm-``opencode serve`` model is gone. The manager
keeps one stateless :class:`CleanupAgent` (just config) and hands out a fresh
:class:`ConvoSession` per continuous PTT session. ``ensure_started`` /
``shutdown`` are retained as no-ops so the registry's best-effort warm/teardown
calls stay valid without a process to manage.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from .cleanup import CleanupAgent
from .session import ConvoSession

log = logging.getLogger("awm.stt.convo.manager")


def _refine_enabled() -> bool:
    """Whether the LLM cleanup runs per cut. Defaults OFF — the ~6s one-shot
    adds too much lag on the submit path for most use; opt in with
    ``CONVO_REFINE=1`` (matching the ``CONVO_PROVIDER``/``CONVO_MODEL`` env
    convention in ``cleanup.py``)."""
    return os.environ.get("CONVO_REFINE", "0").strip().lower() in ("1", "true", "yes")


class ConvoManager:
    def __init__(self) -> None:
        self._agent = CleanupAgent()
        self._refine = _refine_enabled()
        log.info("convo refine %s", "ON (CONVO_REFINE)" if self._refine else "OFF")

    @property
    def refine(self) -> bool:
        """Whether sessions from this manager run the LLM cleanup per cut."""
        return self._refine

    @property
    def agent(self) -> CleanupAgent:
        """The stateless cleanup seam (an agentcore opencode one-shot driver).
        Exposed for in-process consumers that want the same config."""
        return self._agent

    async def ensure_started(self) -> None:
        """No-op: one-shot cleanups spawn the harness per cut, so there is no
        warm server to start. Kept for the registry's best-effort warm call."""
        return None

    def new_session(self) -> ConvoSession:
        return ConvoSession(self._agent, refine=self._refine)

    async def shutdown(self) -> None:
        """No-op: nothing long-lived to tear down (one-shot per cut)."""
        return None


_manager: Optional[ConvoManager] = None


def get_convo_manager() -> ConvoManager:
    global _manager
    if _manager is None:
        _manager = ConvoManager()
    return _manager

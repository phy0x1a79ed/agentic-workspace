"""Mock conversational partner for exercising the convo voice loop.

This is a **test harness**, not part of the convo cleanup subsystem — it is a
deliberately separable module (sibling to ``convo/``) so it can be ripped out
later without touching anything else. It gives the demo page someone to talk to
so the dictation → clean → submit loop can be driven end-to-end.

Unlike the cleanup loop (which opens a fresh opencode session per silence-cut so
turns stay isolated), the chatbot holds **one persistent session** for the whole
conversation, so opencode keeps the turn history server-side and the partner
remembers what was said. It reuses the *same* warm ``opencode serve`` process the
convo manager already owns (see :func:`backend.convo.manager.get_convo_manager`) —
no second server.

Scope: a process-wide singleton conversation, which is fine for the single dev
operator the demo targets; it is not per-user.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from backend.agent import AgentError, OpencodeAgent

log = logging.getLogger("ptt.chatbot")

# The partner replies in free-form text (via ``agent.message_text``), NOT
# structured output. The default model — opencode Zen's deepseek-v4-flash-free —
# is a *thinking* model, and structured output forces a tool_choice it rejects
# ("Thinking mode does not support this tool_choice"). Free text also suits a
# conversational reply better than JSON.

# Prepended to the first user turn only; the persistent session retains it for
# the rest of the conversation. Tells the model it's a test partner so it stays
# brief rather than launching into assistant-style essays.
PERSONA = (
    "You are a concise conversational partner being used to TEST a "
    "voice-dictation system. The human is dictating by speaking; their messages "
    "may be lightly cleaned-up speech. Reply naturally and briefly — a sentence "
    "or two — to keep the conversation going. Do not be verbose, do not use "
    "bullet lists, and do not explain that you are an AI. Just talk.\n\n"
    "First message from the human:\n"
)


class ChatBot:
    """A single persistent-session conversation against the warm opencode agent."""

    def __init__(self, agent: OpencodeAgent) -> None:
        self._agent = agent
        self._session_id: Optional[str] = None
        self._lock = asyncio.Lock()  # serialize turns on the one session
        self._turns = 0

    async def reply(self, user_text: str) -> str:
        """Send one user turn, return the partner's reply.

        Maintained context lives in the persistent ``_session_id`` — it is never
        deleted between turns. Raises :class:`AgentError` on backend failure so
        the RPC layer can degrade gracefully.
        """
        text = (user_text or "").strip()
        if not text:
            return ""
        async with self._lock:
            if self._session_id is None:
                self._session_id = await self._agent.open_session()
                self._turns = 0
            prompt = (PERSONA + text) if self._turns == 0 else text
            reply = await self._agent.message_text(self._session_id, prompt)
            self._turns += 1
            return reply

    async def reset(self) -> None:
        """Drop the current conversation; the next turn starts a fresh session."""
        async with self._lock:
            sid, self._session_id, self._turns = self._session_id, None, 0
        if sid is not None:
            await self._agent.close_session(sid)


_chatbot: Optional[ChatBot] = None


def get_chatbot() -> ChatBot:
    """Process-wide singleton, bound to the convo manager's shared warm agent."""
    global _chatbot
    if _chatbot is None:
        from backend.convo.manager import get_convo_manager

        _chatbot = ChatBot(get_convo_manager().agent)
    return _chatbot

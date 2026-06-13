"""Per-convo-session state + the silence-cut driver (the inner loop).

One :class:`ConvoSession` lives per active continuous-mode PTT agent. It owns
the three logs from the design — ``raw_log`` (verbatim STT, reset on submit),
``composer`` (latest cleaned text), ``notes_pad`` (persists across submits) —
plus the frontend-supplied ``context`` buffer. On each silence-cut it threads
those into the LLM and applies the result.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from backend.agent import AgentError, OpencodeAgent

from .prompt import CONVO_SCHEMA, MAX_CONTEXT_CHARS, build_prompt

log = logging.getLogger("ptt.convo.session")


@dataclass
class ConvoResult:
    """Outcome of one silence-cut, for the service layer to broadcast."""

    cleaned_text: str
    should_submit: bool
    fallback: bool = False  # True when the LLM failed and we emitted raw text


class ConvoSession:
    """The convo inner loop for a single user's continuous session."""

    def __init__(self, agent: OpencodeAgent) -> None:
        self._agent = agent
        self.raw_log: list[str] = []
        self.composer: str = ""
        self.notes_pad: str = ""
        self.context: str = ""
        # Serialize cuts: each cleanup holds the lock across its LLM call so
        # composer/notes updates apply in arrival order even if a slow call
        # overlaps a newer cut.
        self._lock = asyncio.Lock()

    def set_context(self, text: str) -> None:
        """Update the recent-chat-history buffer the frontend ships up."""
        self.context = (text or "")[-MAX_CONTEXT_CHARS:]

    async def on_silence_cut(self, new_raw: str) -> ConvoResult:
        """Run the inner loop for one finalized utterance.

        Appends ``new_raw`` to the raw log, asks the LLM for a faithful clean +
        submit decision, updates state, and returns what to show. On submit,
        resets ``raw_log`` and ``composer`` (notes persist). Never raises — an
        LLM failure falls back to the accumulated raw text, no submit.
        """
        new_raw = (new_raw or "").strip()
        async with self._lock:
            if not new_raw:
                return ConvoResult(self.composer, False)
            prior_raw = " ".join(self.raw_log).strip()
            self.raw_log.append(new_raw)

            prompt = build_prompt(
                prior_raw=prior_raw,
                new_raw=new_raw,
                prev_composer=self.composer,
                notes=self.notes_pad,
                context=self.context,
            )
            try:
                result = await self._agent.complete(prompt, CONVO_SCHEMA)
                cleaned = str(result.get("cleaned_text", "")).strip()
                should_submit = bool(result.get("should_submit", False))
                notes_update = result.get("notes_update")
                fallback = False
            except AgentError as exc:
                # Faithful degradation: show the accumulated raw transcript,
                # don't auto-submit, leave notes untouched.
                log.warning("convo cleanup failed, using raw text: %s", exc)
                cleaned = " ".join(self.raw_log).strip()
                should_submit = False
                notes_update = None
                fallback = True

            if not cleaned:
                cleaned = " ".join(self.raw_log).strip()

            self.composer = cleaned
            if isinstance(notes_update, str) and notes_update.strip():
                self.notes_pad = notes_update.strip()

            if should_submit:
                # Flush: the returned text is the submitted message; clear the
                # working logs so the next utterance starts a fresh message.
                self.raw_log = []
                self.composer = ""

            return ConvoResult(cleaned, should_submit, fallback)

    def reset(self) -> None:
        """Drop all working state (e.g. session torn down). Notes go too —
        they are scoped to a single continuous session."""
        self.raw_log = []
        self.composer = ""
        self.notes_pad = ""
        self.context = ""

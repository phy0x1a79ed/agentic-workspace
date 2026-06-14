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
from typing import Any

from .cleanup import CleanupError
from .prompt import MAX_CONTEXT_CHARS, build_prompt

log = logging.getLogger("awm.stt.convo.session")


@dataclass
class ConvoResult:
    """Outcome of one silence-cut, for the service layer to broadcast."""

    cleaned_text: str
    should_submit: bool
    fallback: bool = False  # True when the LLM failed and we emitted raw text


class ConvoSession:
    """The convo inner loop for a single user's continuous session."""

    def __init__(self, agent: Any) -> None:
        # ``agent`` is any object exposing ``async complete_json(prompt) -> dict``
        # — the production seam is ``awm.stt.convo.cleanup.CleanupAgent`` (an
        # agentcore opencode one-shot per cut); headless tests pass a stub.
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
        submit decision, updates state, and returns what to show. ``should_submit``
        in the result is the LLM's *completeness* judgement only — it does NOT
        flush here. The driver (registry) gates the actual submit on
        "complete AND no new speech" and calls :meth:`take_submission` to flush.
        Never raises — an LLM failure falls back to the accumulated raw text,
        ``should_submit=False``.
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
                # Free-form + JSON-parse, NOT forced structured output: the
                # default Zen model is a *thinking* model that 400s on a forced
                # StructuredOutput tool_choice. agentcore's run_once(schema=...)
                # appends a bare-JSON instruction (no forced tool_choice) and
                # parses the object out, so it works on thinking and non-thinking
                # models alike. See ``cleanup.py`` for the why.
                result = await self._agent.complete_json(prompt)
                cleaned = str(result.get("cleaned_text", "")).strip()
                should_submit = bool(result.get("should_submit", False))
                notes_update = result.get("notes_update")
                fallback = False
            except CleanupError as exc:
                # Faithful degradation: show the accumulated raw transcript,
                # don't auto-submit, leave notes untouched. The exc message
                # distinguishes empty-reply (thinking model put its answer in
                # reasoning parts) from no-JSON-found from HTTP error — keep it
                # visible: it's the single most useful signal for why a live
                # convo never submits.
                log.warning("convo cleanup failed, using raw text: %s", exc)
                cleaned = " ".join(self.raw_log).strip()
                should_submit = False
                notes_update = None
                fallback = True

            if not cleaned:
                cleaned = " ".join(self.raw_log).strip()

            # Per-cut outcome trace — the diagnostic for where submission breaks.
            log.info(
                "convo cut: should_submit=%s fallback=%s cleaned=%r",
                should_submit, fallback, cleaned[:120],
            )

            self.composer = cleaned
            if isinstance(notes_update, str) and notes_update.strip():
                self.notes_pad = notes_update.strip()

            # NOTE: no flush here even when should_submit. The message is only
            # actually sent once the user stops speaking; the driver confirms
            # that and calls take_submission() to flush. Holding state means a
            # user who keeps talking after a "complete" cut just keeps building
            # the same message.
            return ConvoResult(cleaned, should_submit, fallback)

    async def take_submission(self) -> str:
        """Confirm the current composed message as sent: return it and clear the
        working logs so the next utterance starts fresh. Notes persist across
        submits. Lock-shared with :meth:`on_silence_cut` so a flush can't race a
        concurrent cut."""
        async with self._lock:
            text = self.composer
            self.raw_log = []
            self.composer = ""
            return text

    def reset(self) -> None:
        """Drop all working state (e.g. session torn down). Notes go too —
        they are scoped to a single continuous session."""
        self.raw_log = []
        self.composer = ""
        self.notes_pad = ""
        self.context = ""

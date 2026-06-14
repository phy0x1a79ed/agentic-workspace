"""Per-convo-session state — the raw-transcript accumulator.

One :class:`ConvoSession` lives per active continuous-mode PTT agent. Its only
job is to accumulate the raw (whisper) transcript across successive silence-cuts
into a single composer message and flush it on submit. There is no LLM: the
text the user sees is exactly what whisper produced, re-passed accurately on each
cut. Submit timing is owned by the registry's silence poll, not by this object.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("awm.stt.convo.session")


class ConvoSession:
    """The convo inner loop for a single user's continuous session: accumulate
    raw silence-cut text into ``composer`` and flush on submit."""

    def __init__(self) -> None:
        # ``raw_log`` holds each finalized silence-cut chunk verbatim; ``composer``
        # is their join — the message the user is building. Both reset on submit.
        self.raw_log: list[str] = []
        self.composer: str = ""
        # Serialize cuts vs. the submit flush so a flush can't race a concurrent
        # cut (the cut runs in a dispatched task; the flush in the silence poll).
        self._lock = asyncio.Lock()

    async def on_silence_cut(self, new_raw: str) -> str:
        """Append one finalized utterance's raw text and return the composed
        message so far. Does NOT submit — the registry's silence poll decides
        that on confirmed trailing silence and calls :meth:`take_submission`."""
        new_raw = (new_raw or "").strip()
        async with self._lock:
            if new_raw:
                self.raw_log.append(new_raw)
                self.composer = " ".join(self.raw_log).strip()
            log.info("convo cut: composer=%r", self.composer[:120])
            return self.composer

    async def take_submission(self) -> str:
        """Confirm the current composed message as sent: return it and clear the
        working logs so the next utterance starts fresh. Lock-shared with
        :meth:`on_silence_cut` so a flush can't race a concurrent cut."""
        async with self._lock:
            text = self.composer
            self.raw_log = []
            self.composer = ""
            return text

    def reset(self) -> None:
        """Drop all working state (e.g. session torn down)."""
        self.raw_log = []
        self.composer = ""

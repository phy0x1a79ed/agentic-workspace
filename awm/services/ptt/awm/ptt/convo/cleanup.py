"""The convo cleanup LLM seam, on the shared ``awm.agentcore`` harness layer.

The convo inner loop (``ConvoSession``) cleans each silence-cut by sending a
prompt to an LLM and parsing back a small JSON object
(``{cleaned_text, should_submit, notes_update?}``). It does NOT know how the
LLM is reached — it just calls ``agent.complete_json(prompt) -> dict`` on a
seam object. This module is the production seam: it drives
``awm.agentcore.run_once`` in **opencode one-shot** mode.

Why one-shot per cut (not a persistent session): each silence-cut is a fresh,
self-contained cleanup of the whole accumulated utterance — continuity is the
caller's job (it threads ``prior_raw`` / ``notes`` / ``context`` into the
prompt), so there's no cross-turn server-side memory to keep. ``run_once``
opens → sends → drains → closes one harness invocation per call.

Why a *thinking-model-safe* path: the default opencode Zen model
(``deepseek-v4-flash-free``) is a thinking model and rejects a forced
``StructuredOutput`` tool_choice. ``run_once(schema=...)`` appends a bare-JSON
instruction (no forced tool_choice) and parses the first JSON object out of the
reply — the model-agnostic structured path. We surface any failure as
:class:`CleanupError` so :class:`ConvoSession` can fall back to raw text.

The provider/model are overridable via ``CONVO_PROVIDER`` / ``CONVO_MODEL`` so
swapping to a paid tier (e.g. the official DeepSeek API) is a config change, not
a code change — matching the old ``backend.agent`` defaults.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger("awm.ptt.convo.cleanup")

# opencode Zen's free DeepSeek by default; both overridable via env so swapping
# the provider/model (e.g. to a paid tier) stays a config change.
DEFAULT_PROVIDER_ID = os.environ.get("CONVO_PROVIDER", "opencode")
DEFAULT_MODEL_ID = os.environ.get("CONVO_MODEL", "deepseek-v4-flash-free")


class CleanupError(RuntimeError):
    """A cleanup completion could not be produced (harness down, timeout, or no
    parseable JSON). :class:`ConvoSession` catches this and falls back to the
    accumulated raw text with ``should_submit=False``."""


class CleanupAgent:
    """Production cleanup seam: one ``agentcore`` opencode one-shot per cut.

    Exposes the single method ``ConvoSession`` depends on —
    ``async complete_json(prompt) -> dict`` — so the session stays decoupled
    from the harness and the headless tests can substitute a stub with the same
    surface.
    """

    def __init__(
        self,
        *,
        provider_id: str = DEFAULT_PROVIDER_ID,
        model_id: str = DEFAULT_MODEL_ID,
        directory: Optional[str] = None,
    ) -> None:
        self.provider_id = provider_id
        self.model_id = model_id
        # Pure cleanup prompts need no project files; default to the service cwd.
        self.directory = directory or os.getcwd()

    def _config(self):
        # Imported lazily so this module stays import-cheap and the agentcore
        # dependency is only pulled when a cleanup actually runs.
        from awm.agentcore import AgentConfig

        return AgentConfig(
            harness="opencode",
            mode="oneshot",
            params={"provider_id": self.provider_id, "model_id": self.model_id},
            workdir=self.directory,
        )

    async def complete_json(self, prompt_text: str) -> dict:
        """Run one opencode one-shot and return the parsed JSON object.

        The prompt already instructs the model to emit a bare JSON object; we
        also pass the schema so ``run_once`` re-states the shape. Raises
        :class:`CleanupError` on any harness failure or unparseable reply so the
        caller can degrade to raw text.
        """
        from awm.agentcore import run_once

        from .prompt import CONVO_SCHEMA

        try:
            obj = await run_once(self._config(), prompt_text, schema=CONVO_SCHEMA)
        except (ValueError, RuntimeError) as exc:
            raise CleanupError(str(exc)) from exc
        if not isinstance(obj, dict):
            raise CleanupError(f"cleanup reply was not a JSON object: {type(obj)}")
        return obj

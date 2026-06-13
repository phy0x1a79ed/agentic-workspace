"""Liftable LLM backend: structured completions via a warm ``opencode serve``.

This package is intentionally self-contained — it imports nothing from the
ptt service (no ``registry``, no ``convo``, no hub code). Its whole job is to
keep one ``opencode serve`` subprocess warm and turn a (prompt, json-schema)
pair into a validated dict, using opencode Zen's free DeepSeek model by
default. Lift it out by copying the directory; the only deps are ``httpx`` and
the ``opencode`` binary on PATH.

See ``opencode_agent.py`` for the wire contract (verified against the opencode
source: ``POST /session`` → ``POST /session/:id/message`` with a
``format: {type:"json_schema", schema}`` payload → ``info.structured`` →
``DELETE /session/:id``).
"""

from .opencode_agent import (
    AgentError,
    OpencodeAgent,
    DEFAULT_MODEL_ID,
    DEFAULT_PROVIDER_ID,
)

__all__ = [
    "AgentError",
    "OpencodeAgent",
    "DEFAULT_MODEL_ID",
    "DEFAULT_PROVIDER_ID",
]

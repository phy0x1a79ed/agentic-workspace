"""Public entry points: ``open_agent`` (the config factory) + ``run_once``.

These are the single dynamic entry points both callers (the agents service,
the stt cleanup) use. ``open_agent(config)`` selects the backend from
``config.harness`` and threads everything else in. ``run_once`` is generic over
the session — open → send → drain until terminal → close — so a one-shot is
just a wrapped live session.

Leaf module: no awm.config / gateway imports.
"""

from __future__ import annotations

import json
from typing import Any, Optional, Union

from .session import AgentSession
from .types import AgentConfig, AgentEvent


def open_agent(config: AgentConfig) -> AgentSession:
    """Select a backend and return an (unstarted) :class:`AgentSession`.

    The session lazily starts on the first ``send``/``events`` call, or call
    ``await session.start()`` explicitly. ``config.harness`` picks the backend;
    ``config.mode`` is advisory (both backends implement both modes — one-shot
    is realized via :func:`run_once`).
    """
    if config.harness == "claude":
        from .claude_backend import ClaudeSession
        return ClaudeSession(config)
    if config.harness == "opencode":
        from .opencode_backend import OpencodeSession
        return OpencodeSession(config)
    raise ValueError(
        f"Unknown harness {config.harness!r}; expected 'claude' | 'opencode'"
    )


# Sentinel for "no terminal event seen yet".
_TERMINAL_KINDS = ("result", "error")


def _schema_instruction(prompt: str, schema: dict) -> str:
    """Append a bare-JSON instruction so a free-form model emits a parseable
    object (no forced tool_choice — thinking models reject that)."""
    return (
        f"{prompt}\n\n"
        "Respond with ONLY a single bare JSON object (no prose, no code "
        "fences) conforming to this JSON Schema:\n"
        f"{json.dumps(schema)}"
    )


async def run_once(
    config: AgentConfig,
    prompt: str,
    schema: Optional[dict] = None,
) -> Union[str, dict, Any]:
    """One-shot: open → send → drain until terminal → close.

    Returns the assistant's final text (``str``) when ``schema`` is None, or the
    parsed JSON object (``dict``) when ``schema`` is given. Generic over the
    backend — works for both claude and opencode.

    With ``schema``, the prompt is augmented to ask for a bare JSON object and
    the reply is parsed out (the thinking-model-safe path — no forced
    ``StructuredOutput`` tool_choice). Raises ``ValueError`` if a schema was
    requested but no JSON object could be parsed.
    """
    send_text = _schema_instruction(prompt, schema) if schema else prompt
    session = open_agent(config)
    final_text: Optional[str] = None
    messages: list[str] = []
    error_text: Optional[str] = None
    try:
        await session.start()
        # Subscribe BEFORE sending so no early event is lost to a
        # pump-drained-past-us race.
        stream = session.subscribe()
        await session.send(send_text)
        async for event in stream:
            if event.kind == "message" and event.text:
                messages.append(event.text)
            elif event.kind == "result":
                if event.text:
                    final_text = event.text
                break
            elif event.kind == "error":
                error_text = event.text or "agent error"
                break
    finally:
        await session.close()

    if error_text is not None:
        raise RuntimeError(f"run_once failed: {error_text}")

    text = final_text if final_text is not None else "\n".join(messages).strip()

    if schema is None:
        return text

    # Schema path: parse the first balanced JSON object out of the reply.
    from .opencode_backend import _extract_json_object
    obj = _extract_json_object(text)
    if obj is None:
        raise ValueError(
            f"run_once(schema=...) got no parseable JSON object in reply: "
            f"{text[:200]!r}"
        )
    return obj

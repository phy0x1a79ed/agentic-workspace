"""Question-vs-idle classification for turn-end events.

A turn end ("the agent stopped working") needs attention either way, but a
turn whose final assistant message is a question / request for input is a
stronger signal than one that simply finished. Nothing upstream distinguishes
the two — the Claude ``Stop`` hook and OpenCode's ``session.idle`` fire on
every turn end — so this module classifies from the last assistant message.

For Claude the producer hook sends ``transcript_path`` (it must NOT read the
transcript itself: the transcript JSONL is known to lag the Stop hook — the
flush race — and a hook that lingers stalls the turn). ``read_last_assistant``
does the read server-side with a short retry to ride out that race. OpenCode's
plugin passes ``last_message`` inline via its client SDK, so no file read.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Optional

# Tool names whose use in the final message means "the agent is asking the
# user something" regardless of the message text.
_ASK_TOOLS = {"askuserquestion", "ask_user_question", "exitplanmode"}

# Interrogative / input-requesting lead-ins for the last line of a message
# that doesn't literally end with a question mark.
_ASK_LEADINS = re.compile(
    r"^(should i|shall i|do you want|would you like|which |what |how should"
    r"|let me know|please (confirm|choose|pick|provide|clarify|review)"
    r"|can you (confirm|clarify|provide)|waiting for your)",
    re.IGNORECASE,
)

_TAIL_BYTES = 256 * 1024  # transcript read window — turns live at the end


def is_question(text: Optional[str], *, tool_names: tuple[str, ...] = ()) -> bool:
    """Heuristic: does this final assistant message await user input?"""
    if any((n or "").lower() in _ASK_TOOLS for n in tool_names):
        return True
    if not text:
        return False
    stripped = text.rstrip().rstrip("*_`")  # markdown emphasis around a trailing ?
    if stripped.endswith("?"):
        return True
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if not lines:
        return False
    last = lines[-1].lstrip("-*>#0123456789. ").rstrip("*_`")
    if last.endswith("?"):
        return True
    return bool(_ASK_LEADINS.match(last))


def _last_assistant_from_lines(lines: list[str]) -> tuple[Optional[str], tuple[str, ...]]:
    """Scan transcript JSONL lines for the LAST assistant message's text + tool names.

    Mirrors the entry shape agentcore's tmux backend parses:
    ``{"type": "assistant", "message": {"content": [{"type": "text"|"tool_use", ...}]}}``.
    """
    text: Optional[str] = None
    tools: tuple[str, ...] = ()
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            parsed = json.loads(ln)
        except Exception:
            continue
        if not isinstance(parsed, dict) or parsed.get("type") != "assistant":
            continue
        msg = parsed.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        chunk_text: list[str] = []
        chunk_tools: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and (block.get("text") or "").strip():
                chunk_text.append(block["text"].strip())
            elif block.get("type") == "tool_use":
                chunk_tools.append(block.get("name") or "")
        if chunk_text:
            text = "\n".join(chunk_text)
            tools = tuple(chunk_tools)
        elif chunk_tools:
            tools = tuple(chunk_tools)
    return text, tools


def _read_tail(path: str) -> list[str]:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - _TAIL_BYTES))
        data = f.read()
    return data.decode("utf-8", errors="replace").splitlines()


async def read_last_assistant(
    transcript_path: Optional[str],
    *,
    retries: int = 4,
    delay: float = 0.35,
) -> tuple[Optional[str], tuple[str, ...]]:
    """Best-effort last assistant message from a Claude transcript JSONL.

    Retries briefly: the transcript can lag the Stop hook (independent flush
    timing). Never raises — a missing/garbled transcript yields ``(None, ())``
    and the caller classifies the turn as plain ``idle``.
    """
    if not transcript_path:
        return None, ()
    for attempt in range(retries):
        try:
            text, tools = _last_assistant_from_lines(_read_tail(transcript_path))
        except OSError:
            text, tools = None, ()
        if text is not None:
            return text, tools
        if attempt < retries - 1:
            await asyncio.sleep(delay)
    return None, ()

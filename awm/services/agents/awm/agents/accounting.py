"""Transcript accounting for the fleet roster (absorbed from notifications).

Two concerns, both pure functions over a Claude transcript JSONL:

- **Last-message read** (``read_last_assistant`` / ``is_question``) — the
  server-side read of a Stop-hook's transcript, with a short retry to ride out
  the transcript-flush race (the JSONL lags the Stop hook). ``is_question`` is a
  retained heuristic util; the roster's status is grounded purely in hook
  signals and does not fork on it.
- **Token / EOOT accounting** (``accumulate_usage`` + ``eoot``) —
  ``accumulate_usage`` folds only the transcript bytes appended since the last
  turn into the five rate-weighted token categories (append-only JSONL, so a
  byte-offset high-water mark is both correct and cheap). ``eoot`` prices a
  session's cumulative tokens as Equivalent Opus-4.8 Output Tokens from a
  configurable rate table: ``EOOT = Σ(tokens_cat × rate_cat) ÷ divisor``.

These are shared leaves: both the roster (fleet observe plane) and any future
transcript consumer feed off them. No service/DB state here.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Optional

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Last assistant message read + question heuristic
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Token / context accounting (incremental, byte-offset high-water mark)
# ---------------------------------------------------------------------------


def _usage_of(entry: dict) -> Optional[dict]:
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return None
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    return usage if isinstance(usage, dict) else None


def _cat_tokens(usage: dict) -> dict[str, int]:
    """Break a ``message.usage`` into the five rate-weighted categories."""
    cc = usage.get("cache_creation") or {}
    cw5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
    cw1 = int(cc.get("ephemeral_1h_input_tokens") or 0)
    total_cc = int(usage.get("cache_creation_input_tokens") or 0)
    # If only the total is present (no breakdown), attribute it to the 5m bucket.
    if not cw5 and not cw1 and total_cc:
        cw5 = total_cc
    return {
        "tok_in": int(usage.get("input_tokens") or 0),
        "tok_out": int(usage.get("output_tokens") or 0),
        "tok_cache_write_5m": cw5,
        "tok_cache_write_1h": cw1,
        "tok_cache_read": int(usage.get("cache_read_input_tokens") or 0),
    }


def accumulate_usage(transcript_path: Optional[str], from_offset: int) -> Optional[dict]:
    """Scan new transcript bytes since ``from_offset``; return usage deltas.

    Claude transcripts are append-only JSONL, so summing token categories over
    just the bytes appended since the last turn is both correct and cheap (no
    re-scan of the whole file each turn). Returns::

        {"add": {tok_in, tok_out, tok_cache_write_5m, tok_cache_write_1h,
                 tok_cache_read},   # increments to add to the cumulative row
         "context_tokens": int,     # last turn's live context size, or None
         "model": str | None,       # last turn's model id
         "new_offset": int}         # advance the high-water mark to here

    ``None`` when there's nothing to read (no path / no new complete lines).
    A file shorter than ``from_offset`` (rotated / truncated) resets to 0.
    Never raises.
    """
    if not transcript_path:
        return None
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = from_offset if 0 <= from_offset <= size else 0
            if start >= size:
                return None
            f.seek(start)
            data = f.read()
    except OSError:
        return None

    # Only consume up to the last newline; keep any partial trailing line for
    # the next pass (offset stays before it).
    nl = data.rfind(b"\n")
    if nl < 0:
        return None
    consumed = data[: nl + 1]
    new_offset = start + len(consumed)

    add = {"tok_in": 0, "tok_out": 0, "tok_cache_write_5m": 0,
           "tok_cache_write_1h": 0, "tok_cache_read": 0}
    context_tokens: Optional[int] = None
    model: Optional[str] = None
    for ln in consumed.decode("utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            entry = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        usage = _usage_of(entry)
        if usage is None:
            continue
        cats = _cat_tokens(usage)
        for k, v in cats.items():
            add[k] += v
        # Live context size = what was in-context for this turn.
        context_tokens = (
            cats["tok_in"] + cats["tok_cache_read"]
            + int(usage.get("cache_creation_input_tokens") or 0)
        )
        m = (entry.get("message") or {}).get("model")
        if m:
            model = m

    return {"add": add, "context_tokens": context_tokens,
            "model": model, "new_offset": new_offset}


# ---------------------------------------------------------------------------
# EOOT pricing (Equivalent Opus-4.8 Output Tokens)
# ---------------------------------------------------------------------------


class ModelRate(BaseModel):
    """USD per million tokens, by category (OpenRouter basis)."""

    input: float = 0.0
    output: float = 0.0
    cache_write_5m: float = 0.0
    cache_write_1h: float = 0.0
    cache_read: float = 0.0


# Substring-keyed: a transcript model id ("claude-opus-4-8[1m]") is matched to a
# rate by the first key that appears in its lowercased form.
DEFAULT_RATES: dict[str, ModelRate] = {
    "opus":   ModelRate(input=5,  output=25, cache_write_5m=6.25,  cache_write_1h=10, cache_read=0.50),
    "sonnet": ModelRate(input=2,  output=10, cache_write_5m=2.50,  cache_write_1h=4,  cache_read=0.20),
    "fable":  ModelRate(input=10, output=50, cache_write_5m=12.50, cache_write_1h=20, cache_read=1.00),
    "haiku":  ModelRate(input=1,  output=5,  cache_write_5m=1.25,  cache_write_1h=2,  cache_read=0.10),
}


def rate_for_model(rates: dict[str, ModelRate], model: Optional[str]) -> Optional[ModelRate]:
    """Match a transcript model id to a rate by substring; None if unknown."""
    if not model:
        return None
    m = model.lower()
    for key, rate in rates.items():
        if key.lower() in m:
            return rate
    return None


def eoot(
    tok_in: int, tok_out: int, tok_cw5: int, tok_cw1: int, tok_cr: int,
    *, model: Optional[str], rates: dict[str, ModelRate], divisor: float,
) -> Optional[float]:
    """Equivalent Opus-output tokens for a session's cumulative usage.

    ``EOOT = Σ(tokens_cat × rate_cat) ÷ divisor`` — token units cancel to
    "Opus-4.8 output tokens" (both rates and divisor are $/MTok). Returns None
    when the model is unknown (can't price it) so the UI can show a dash rather
    than a misleading zero.
    """
    rate = rate_for_model(rates, model)
    if rate is None or divisor <= 0:
        return None
    cost_num = (
        tok_in * rate.input
        + tok_out * rate.output
        + tok_cw5 * rate.cache_write_5m
        + tok_cw1 * rate.cache_write_1h
        + tok_cr * rate.cache_read
    )
    return cost_num / divisor

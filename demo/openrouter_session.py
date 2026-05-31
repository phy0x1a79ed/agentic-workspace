"""OpenRouter chat-completions backend with the same async interface as
``ClaudeSession``.

Selected when ``LLM_BACKEND=openrouter`` (the default). Holds conversation
history in-process and streams SSE deltas from OpenRouter's OpenAI-compatible
``/chat/completions`` endpoint.

Tool support: if the chosen model advertises tool use, we wire the
``show()`` MCP equivalent as an OpenAI-format function tool. Models without
tool support (most free chat models) just fall back to prose — the persona
file already nudges them to be voice-friendly.

Yields the same ``(kind, body)`` tuples as ClaudeSession:
  - ("text", delta)
  - ("show", json)         — only if model used the show tool
  - ("result", "")          — end-of-turn marker
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx


log = logging.getLogger("voice-demo.openrouter")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

DEFAULT_MODEL = os.environ.get("OPENROUTER_MODEL", "z-ai/glm-4.5-air:free")
DEFAULT_PERSONA = Path(__file__).parent / "voice-agent.md"

SHOW_TOOL = {
    "type": "function",
    "function": {
        "name": "show",
        "description": (
            "Display content to the user as a visual aside (not spoken). "
            "Use for code, file paths, URLs, exact identifiers — anything "
            "the voice channel can't carry well. Keep voice responses in "
            "prose; this is the side channel."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Text to show."},
                "kind": {
                    "type": "string",
                    "enum": ["code", "link", "path", "text"],
                    "description": "Rendering hint.",
                },
            },
            "required": ["content"],
        },
    },
}


class OpenRouterSession:
    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        persona_path: Optional[Path] = None,
        log_path: Optional[Path] = None,
    ):
        self.model = model or DEFAULT_MODEL
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.persona_path = Path(
            persona_path or os.environ.get("VOICE_AGENT_PROMPT", DEFAULT_PERSONA)
        )
        self.log_path = log_path
        self._log_fp = None

        self.messages: list[dict] = []
        self.tools_enabled: bool = False
        self._client: Optional[httpx.AsyncClient] = None
        self._events: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None

    # ---------- lifecycle ----------

    async def start(self) -> None:
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "HTTP-Referer": "https://github.com/awm-voice-demo",
                "X-Title": "awm voice demo",
            },
        )
        # Seed conversation with persona as a system message.
        if self.persona_path.exists():
            persona = self.persona_path.read_text(encoding="utf-8").strip()
            if persona:
                self.messages.append({"role": "system", "content": persona})

        # Probe model capabilities (tool support).
        self.tools_enabled = await self._probe_tool_support()
        log.info(
            "openrouter session: model=%s tools=%s",
            self.model, self.tools_enabled,
        )
        if self.log_path is not None:
            self._log_fp = open(self.log_path, "ab")
            self._log_fp.write(
                f"--- openrouter session start (model={self.model}, tools={self.tools_enabled}) ---\n".encode()
            )

    async def stop(self) -> None:
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._log_fp is not None:
            self._log_fp.close()
            self._log_fp = None

    # ---------- protocol ----------

    async def send(self, text: str) -> None:
        if self._client is None:
            raise RuntimeError("session not started")
        # If a previous turn is somehow still running, cancel it first.
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):
                pass
        self.messages.append({"role": "user", "content": text})
        self._worker = asyncio.create_task(self._run_turn())

    async def events(self) -> AsyncIterator[tuple[str, str]]:
        while True:
            item = await self._events.get()
            if item is None:
                return
            yield item

    # ---------- internals ----------

    async def _probe_tool_support(self) -> bool:
        assert self._client is not None
        try:
            r = await self._client.get(OPENROUTER_MODELS_URL, timeout=10.0)
            r.raise_for_status()
            data = r.json().get("data", [])
            for m in data:
                if m.get("id") == self.model:
                    sp = m.get("supported_parameters") or []
                    return "tools" in sp
        except Exception as exc:  # noqa: BLE001
            log.warning("model capability probe failed: %s", exc)
        return False

    async def _run_turn(self) -> None:
        """One assistant turn. May loop if the model emits tool calls."""
        try:
            # Tool-call loop: model may call show() one or more times, each
            # time we append a tool-response and re-prompt. Cap iterations to
            # avoid pathological cases.
            for _ in range(4):
                tool_calls = await self._stream_one_request()
                if not tool_calls:
                    break
                # The streamed request already appended assistant message
                # with the tool_calls to self.messages and emitted ("show",..)
                # events; now append tool responses and re-prompt.
                for call in tool_calls:
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": call["name"],
                        "content": "shown",
                    })
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("openrouter turn failed")
            await self._events.put(("text", f"\n[openrouter error: {exc}]"))
        finally:
            await self._events.put(("result", ""))

    async def _stream_one_request(self) -> list[dict]:
        """Run one streamed chat completion. Returns any tool calls the
        assistant made (empty list if it produced only text)."""
        assert self._client is not None
        body: dict = {
            "model": self.model,
            "messages": self.messages,
            "stream": True,
        }
        if self.tools_enabled:
            body["tools"] = [SHOW_TOOL]

        text_buf: list[str] = []
        # OpenAI streaming tool-calls arrive as deltas indexed by call index;
        # we accumulate them.
        tool_call_acc: dict[int, dict] = {}

        async with self._client.stream(
            "POST", OPENROUTER_URL, json=body,
        ) as resp:
            if resp.status_code != 200:
                err_body = (await resp.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(f"openrouter {resp.status_code}: {err_body[:400]}")

            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if self._log_fp:
                    self._log_fp.write((data + "\n").encode("utf-8"))
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                # Text delta.
                if (content := delta.get("content")):
                    text_buf.append(content)
                    await self._events.put(("text", content))
                # Tool call deltas — OpenAI-style streaming.
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = tool_call_acc.setdefault(idx, {
                        "id": "", "name": "", "arguments": "",
                    })
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]

        # Finalize: append assistant message to history.
        assistant_msg: dict = {"role": "assistant", "content": "".join(text_buf)}
        finished_tool_calls: list[dict] = []
        if tool_call_acc:
            assistant_msg["tool_calls"] = []
            for _, slot in sorted(tool_call_acc.items()):
                assistant_msg["tool_calls"].append({
                    "id": slot["id"] or f"call_{len(finished_tool_calls)}",
                    "type": "function",
                    "function": {
                        "name": slot["name"],
                        "arguments": slot["arguments"] or "{}",
                    },
                })
                if slot["name"] == "show":
                    try:
                        args = json.loads(slot["arguments"] or "{}")
                    except json.JSONDecodeError:
                        args = {"content": slot["arguments"], "kind": "text"}
                    payload = json.dumps({
                        "content": str(args.get("content", "")),
                        "kind": str(args.get("kind", "text")),
                    })
                    await self._events.put(("show", payload))
                finished_tool_calls.append(slot)
        self.messages.append(assistant_msg)
        return finished_tool_calls

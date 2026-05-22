"""Persistent `claude` subprocess wrapper for the voice demo.

Mirrors the stream-json framing used by awm/services/sessions_live.py so
the next iteration can swap this wrapper for a rooms WS attach without
changing the audio half of the system.

stdin frame:  {"type":"user","message":{"role":"user","content":"<text>"}}\\n
stdout: one JSON event per line. Events we care about:
  - {"type":"assistant","message":{"content":[{"type":"text","text":"..."}, ...]}}
  - {"type":"result","result":"..."}              (final turn summary)
Everything else is logged and ignored.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import AsyncIterator, Optional


_DEFAULT_PERSONA = Path(__file__).parent / "voice-agent.md"

_TOOL_ARG_LIMIT = 80
_TOOL_RESULT_LIMIT = 240


def _format_tool_use(block: dict) -> str:
    name = block.get("name", "?")
    args = block.get("input") or {}
    if not isinstance(args, dict) or not args:
        return f"{name}()"
    parts = []
    for k, v in args.items():
        v_repr = str(v)
        if len(v_repr) > _TOOL_ARG_LIMIT:
            v_repr = v_repr[:_TOOL_ARG_LIMIT] + "…"
        v_repr = v_repr.replace("\n", "⏎")
        parts.append(f"{k}={v_repr}")
    return f"{name}({', '.join(parts)})"


def _format_tool_result(block: dict) -> str:
    body = block.get("content", "")
    if isinstance(body, list):
        body = " ".join(
            c.get("text", "") for c in body if isinstance(c, dict)
        )
    snippet = str(body).strip().replace("\n", " ⏎ ")
    if len(snippet) > _TOOL_RESULT_LIMIT:
        snippet = snippet[:_TOOL_RESULT_LIMIT] + "…"
    return snippet or "(empty)"


_MCP_CONFIG = Path(__file__).parent / "mcp-config.json"
SHOW_TOOL_NAME = "mcp__show__show"


def _build_argv() -> list[str]:
    argv = [
        "claude",
        "--print",
        "--verbose",
        "--input-format=stream-json",
        "--output-format=stream-json",
        "--include-partial-messages",
    ]
    persona_path = Path(os.environ.get("VOICE_AGENT_PROMPT", _DEFAULT_PERSONA))
    if persona_path.exists():
        text = persona_path.read_text(encoding="utf-8").strip()
        if text:
            argv += ["--append-system-prompt", text]
    if _MCP_CONFIG.exists():
        argv += [
            "--mcp-config", str(_MCP_CONFIG),
            "--allowed-tools", SHOW_TOOL_NAME,
        ]
    return argv


class ClaudeSession:
    def __init__(self, cwd: Optional[Path] = None, log_path: Optional[Path] = None):
        self.cwd = cwd or Path.cwd()
        self.log_path = log_path
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._log_fp = None

    async def start(self) -> None:
        if self.log_path is not None:
            self._log_fp = open(self.log_path, "ab")
            stderr = self._log_fp
        else:
            stderr = asyncio.subprocess.DEVNULL
        self.proc = await asyncio.create_subprocess_exec(
            *_build_argv(),
            cwd=str(self.cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
            start_new_session=True,
        )

    async def send(self, text: str) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("session not started")
        payload = {
            "type": "user",
            "message": {"role": "user", "content": text},
        }
        line = (json.dumps(payload) + "\n").encode("utf-8")
        self.proc.stdin.write(line)
        await self.proc.stdin.drain()

    async def events(self) -> AsyncIterator[tuple[str, str]]:
        """Yield (kind, body) tuples from stdout.

        kinds:
          - "text"        — assistant text content (one block per yield)
          - "tool_use"    — tool invocation: body is "name(arg=val, ...)"
          - "tool_result" — tool output: body is a truncated text snippet
          - "result"      — turn-end marker (body is empty or final result text)
          - "raw"         — non-JSON line, for debugging
        """
        if self.proc is None or self.proc.stdout is None:
            raise RuntimeError("session not started")
        stdout = self.proc.stdout
        while True:
            line = await stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                yield ("raw", text)
                continue
            t = parsed.get("type")
            if t == "assistant":
                content = parsed.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            body = block.get("text", "")
                            if body:
                                yield ("text", body)
                        elif btype == "tool_use":
                            if block.get("name") == SHOW_TOOL_NAME:
                                args = block.get("input") or {}
                                payload = json.dumps({
                                    "content": str(args.get("content", "")),
                                    "kind": str(args.get("kind", "text")),
                                })
                                yield ("show", payload)
                            else:
                                yield ("tool_use", _format_tool_use(block))
            elif t == "user":
                content = parsed.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_result":
                            # Suppress the canned "shown" result from our
                            # show() tool — it's noise in the transcript.
                            body_text = ""
                            raw = block.get("content", "")
                            if isinstance(raw, list):
                                body_text = " ".join(
                                    c.get("text", "") for c in raw
                                    if isinstance(c, dict)
                                )
                            else:
                                body_text = str(raw)
                            if body_text.strip() == "shown":
                                continue
                            yield ("tool_result", _format_tool_result(block))
            elif t == "result":
                body = parsed.get("result") or ""
                yield ("result", body if isinstance(body, str) else "")

    async def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.stdin is not None and not self.proc.stdin.is_closing():
            try:
                self.proc.stdin.close()
            except Exception:
                pass
        try:
            self.proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
            await self.proc.wait()
        if self._log_fp is not None:
            self._log_fp.close()
            self._log_fp = None

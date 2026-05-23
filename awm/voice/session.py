"""Persistent `claude` subprocess wrapper for the voice agent.

Mirrors the stream-json framing used by awm/services/agent_instances.py.
Bundles two MCP servers:

- ``show``: stdio script in this package, lets the agent emit visual-
  only content (rendered in the side panel; not spoken).
- ``awm``: the standard awm-mcp client, gives the agent access to
  ``room_post``/``room_list``/``room_get``/``room_history``/``room_invite``
  so it can relay voice commands into AWM rooms it has joined.

The MCP config is generated on the fly so paths stay absolute and
package-relative without a checked-in config file.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import AsyncIterator, Optional


_PACKAGE_DIR = Path(__file__).resolve().parent
_DEFAULT_PERSONA = _PACKAGE_DIR / "voice-agent.md"
_SHOW_SCRIPT = _PACKAGE_DIR / "show_mcp.py"

_TOOL_ARG_LIMIT = 80
_TOOL_RESULT_LIMIT = 240

SHOW_TOOL_NAME = "mcp__show__show"
AWM_ROOM_TOOL_NAMES = [
    "mcp__awm__room_list",
    "mcp__awm__room_get",
    "mcp__awm__room_post",
    "mcp__awm__room_history",
    "mcp__awm__room_invite",
    "mcp__awm__room_search",
    "mcp__awm__room_agents",
    "mcp__awm__agent_control",
    "mcp__awm__awm_status",
]


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


def _build_mcp_config() -> Path:
    workspace = os.environ.get(
        "AWM_WORKSPACE", "/home/tony/agentic_workspace"
    )
    cfg = {
        "mcpServers": {
            "show": {
                "command": "mamba",
                "args": [
                    "run", "-n", "awm",
                    "python", str(_SHOW_SCRIPT),
                ],
            },
            "awm": {
                "command": "mamba",
                "args": ["run", "-n", "awm", "awm-mcp"],
                "env": {"AWM_WORKSPACE": workspace},
            },
        }
    }
    fd, path = tempfile.mkstemp(prefix="voice-mcp-", suffix=".json")
    with os.fdopen(fd, "w") as fp:
        json.dump(cfg, fp)
    return Path(path)


def _build_argv(mcp_config: Path) -> list[str]:
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
    allowed = ",".join([SHOW_TOOL_NAME, *AWM_ROOM_TOOL_NAMES])
    argv += [
        "--mcp-config", str(mcp_config),
        "--allowed-tools", allowed,
    ]
    return argv


class ClaudeSession:
    def __init__(self, cwd: Optional[Path] = None, log_path: Optional[Path] = None):
        self.cwd = cwd or Path.cwd()
        self.log_path = log_path
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._log_fp = None
        self._mcp_config_path: Optional[Path] = None

    async def start(self) -> None:
        if self.log_path is not None:
            self._log_fp = open(self.log_path, "ab")
            stderr = self._log_fp
        else:
            stderr = asyncio.subprocess.DEVNULL
        self._mcp_config_path = _build_mcp_config()
        self.proc = await asyncio.create_subprocess_exec(
            *_build_argv(self._mcp_config_path),
            cwd=str(self.cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
            start_new_session=True,
        )

    async def send(self, text: str) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("session not started")
        payload = {"type": "user", "message": {"role": "user", "content": text}}
        line = (json.dumps(payload) + "\n").encode("utf-8")
        self.proc.stdin.write(line)
        await self.proc.stdin.drain()

    async def events(self) -> AsyncIterator[tuple[str, str]]:
        """Yield (kind, body) tuples from stdout.

        kinds: "text" | "tool_use" | "tool_result" | "show" | "result" | "raw"
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
        if self._mcp_config_path is not None:
            try:
                self._mcp_config_path.unlink()
            except OSError:
                pass
            self._mcp_config_path = None

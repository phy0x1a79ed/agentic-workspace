"""Claude Code backend — subprocess + stream-json → :class:`AgentEvent`.

Lifted from ``awm.services.agents.awm.agents.agent_instances`` — specifically
``_build_claude_argv`` (argv), ``_reader_loop`` (stdout drain), and
``_extract_renderable`` (block classification) — and reshaped to emit
normalized events instead of posting to a scope channel.

Both modes use the SAME ``claude --print --output-format=stream-json`` parsing
path: one-shot is just "send one turn, drain until the terminal ``result``
event, close" (NOT ``claude -p`` plain text). The persistent stdin is a
``stream-json`` input pump (``{"type":"user","message":{...}}`` per line), so
live mode keeps feeding turns to the same process.

Leaf module: no awm.config / gateway imports.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import AsyncIterator, Optional

from ._path import resolve_bin
from .session import AgentSession
from .types import AgentConfig, AgentEvent


_VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def build_claude_argv(config: AgentConfig) -> list[str]:
    """Construct the ``claude`` argv from an :class:`AgentConfig`.

    Full-open perms (``permissions='full'``) →
    ``--permission-mode=bypassPermissions``. ``params['effort']`` →
    ``--effort``. ``mcp_config`` → ``--strict-mcp-config --mcp-config <path>``.
    ``allowed_tools`` / ``disallowed_tools`` → ``--allowedTools`` /
    ``--disallowedTools`` (comma-joined): scope which built-in AND MCP tools the
    subprocess may call. Note these gate *tool availability*, which is
    orthogonal to ``--permission-mode`` (the latter gates approval prompts);
    a full-open worker still only sees the tools its allowlist names.
    """
    perm_mode = (
        "bypassPermissions" if config.permissions == "full" else "default"
    )
    argv = [
        resolve_bin("claude"), "--print", "--verbose",
        "--input-format=stream-json", "--output-format=stream-json",
        "--include-partial-messages",
        f"--permission-mode={perm_mode}",
    ]
    if config.model:
        argv.extend(["--model", config.model])
    effort = (config.params or {}).get("effort")
    if effort:
        if effort not in _VALID_EFFORTS:
            raise ValueError(
                f"Invalid effort {effort!r}; choices: {list(_VALID_EFFORTS)}"
            )
        argv.extend(["--effort", effort])
    if config.system_prompt:
        argv.extend(["--append-system-prompt", config.system_prompt])
    if config.allowed_tools:
        argv.extend(["--allowedTools", ",".join(config.allowed_tools)])
    if config.disallowed_tools:
        argv.extend(["--disallowedTools", ",".join(config.disallowed_tools)])
    if config.resume_id:
        argv.extend(["--resume", config.resume_id])
    if config.mcp_config and os.path.exists(config.mcp_config):
        argv.extend(["--strict-mcp-config", "--mcp-config", config.mcp_config])
    return argv


def _classify(parsed: dict, cur_msg_id: str | None = None) -> list[AgentEvent]:
    """Map one parsed claude stream-json object → zero-or-more AgentEvents.

    Mirrors the monolith's ``_extract_renderable`` classification, extended to
    carry structured ``data`` and to cover ``stream_event`` partials, the
    ``system/init`` status, and the terminal ``result``.

    ``cur_msg_id`` is the id of the claude ``message`` currently streaming (set
    from the ``message_start`` frame by :meth:`ClaudeSession._event_source`). It
    is stamped as ``data['message_id']`` on each ``partial`` so the downstream
    fold coalesces a turn's token deltas into one growing bubble. The terminal
    ``message`` / ``tool_use`` events carry the same id read straight off
    ``parsed['message']['id']`` — the stable per-turn key end-to-end.
    """
    out: list[AgentEvent] = []
    t = parsed.get("type")

    if t == "system" and parsed.get("subtype") == "init":
        out.append(AgentEvent(
            kind="status",
            text=None,
            data={
                "subtype": "init",
                "session_id": parsed.get("session_id"),
                "model": parsed.get("model"),
                "slash_commands": parsed.get("slash_commands"),
            },
        ))
        return out

    if t == "stream_event":
        # Partial token deltas: claude emits content_block_delta events when
        # --include-partial-messages is set. Coalesce downstream by message id.
        ev = parsed.get("event") or {}
        if ev.get("type") == "content_block_delta":
            delta = ev.get("delta") or {}
            piece = delta.get("text") or delta.get("partial_json")
            if piece:
                out.append(AgentEvent(
                    kind="partial", text=piece,
                    data={"index": ev.get("index"),
                          "message_id": cur_msg_id},
                ))
        return out

    if t == "assistant":
        # One claude assistant message may carry several content blocks (text +
        # tool_use) under a single message.id — that id is the per-turn bubble
        # key the downstream fold groups by (accumulate text across blocks).
        msg = parsed.get("message", {})
        msg_id = msg.get("id")
        content = msg.get("content", [])
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = (block.get("text") or "").strip()
                if text:
                    out.append(AgentEvent(
                        kind="message", text=text,
                        data={"message_id": msg_id},
                    ))
            elif btype == "tool_use":
                name = block.get("name", "?")
                out.append(AgentEvent(
                    kind="tool_use",
                    text=f"[tool_use: {name}]",
                    data={
                        "name": name,
                        "id": block.get("id"),
                        "input": block.get("input"),
                        "message_id": msg_id,
                    },
                ))
        return out

    if t == "user":
        content = parsed.get("message", {}).get("content", [])
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                body_field = block.get("content", "")
                if isinstance(body_field, list):
                    body_field = " ".join(
                        c.get("text", "") for c in body_field
                        if isinstance(c, dict)
                    )
                snippet = str(body_field).strip()
                if len(snippet) > 240:
                    snippet = snippet[:240] + "…"
                out.append(AgentEvent(
                    kind="tool_result",
                    text=snippet or "[tool_result: empty]",
                    data={
                        "tool_use_id": block.get("tool_use_id"),
                        "is_error": block.get("is_error", False),
                    },
                ))
        return out

    if t == "result":
        is_error = bool(parsed.get("is_error"))
        body = parsed.get("result")
        if is_error:
            out.append(AgentEvent(
                kind="error",
                text=(body.strip() if isinstance(body, str) else None)
                     or "[result error]",
                data={"usage": parsed.get("usage")},
            ))
        else:
            out.append(AgentEvent(
                kind="result",
                text=body if isinstance(body, str) else None,
                data={
                    "usage": parsed.get("usage"),
                    "session_id": parsed.get("session_id"),
                    "num_turns": parsed.get("num_turns"),
                },
            ))
        return out

    return out


class ClaudeSession(AgentSession):
    """One ``claude`` subprocess driven over stream-json."""

    def __init__(self, config: AgentConfig) -> None:
        super().__init__(config)
        self._proc: Optional[asyncio.subprocess.Process] = None
        self.session_id: Optional[str] = config.resume_id
        # The id of the message currently streaming, set off `message_start`;
        # stamped onto each `partial` so a turn's deltas coalesce by message id.
        self._cur_message_id: Optional[str] = None

    async def _start(self) -> None:
        argv = build_claude_argv(self.config)
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.config.workdir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

    async def send(self, text: str) -> None:
        if not self._started:
            await self.start()
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            raise RuntimeError("claude session stdin not available")
        payload = {
            "type": "user",
            "message": {"role": "user", "content": text},
        }
        line = (json.dumps(payload) + "\n").encode("utf-8")
        proc.stdin.write(line)
        await proc.stdin.drain()

    async def _event_source(self) -> AsyncIterator[AgentEvent]:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        stdout = proc.stdout
        while True:
            raw = await stdout.readline()
            if not raw:
                break
            text = raw.decode("utf-8", errors="replace").rstrip("\n")
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                # Non-JSON line: surface as a raw status so nothing is lost.
                yield AgentEvent(kind="status", text=text, data={"raw": True})
                continue
            # Track the session id off the init event for resume.
            if (parsed.get("type") == "system"
                    and parsed.get("subtype") == "init"):
                sid = parsed.get("session_id")
                if isinstance(sid, str):
                    self.session_id = sid
            # Track the streaming message id off `message_start` so the partials
            # that follow are stamped with it (claude's content_block_delta
            # frames don't carry the message id themselves).
            if parsed.get("type") == "stream_event":
                ev = parsed.get("event") or {}
                if ev.get("type") == "message_start":
                    mid = (ev.get("message") or {}).get("id")
                    if isinstance(mid, str):
                        self._cur_message_id = mid
            for event in _classify(parsed, self._cur_message_id):
                yield event

    async def close(self) -> None:
        if self._closed:
            return
        proc = self._proc
        if proc is not None:
            if proc.stdin is not None and not proc.stdin.is_closing():
                try:
                    proc.stdin.close()
                except Exception:  # noqa: BLE001
                    pass
            if proc.returncode is None:
                try:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        proc.kill()
                except ProcessLookupError:
                    pass
        await self._stop_pump()

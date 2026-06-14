"""Opencode backend — warm ``opencode serve`` + parts → :class:`AgentEvent`.

Lifted from ``awm.services.stt.backend.agent.opencode_agent.OpencodeAgent``
(warm ``opencode serve`` subprocess; ``POST /session`` / ``POST
/session/{id}/message`` / ``DELETE /session/{id}``) and reshaped so opencode's
message ``parts`` are mapped onto :class:`AgentEvent` **explicitly** — the old
code worked by coincidence of Claude's shape; here the mapping is deliberate.

Modes:
- live    — one persistent server-side session held across turns; each
            ``send(text)`` POSTs a message and the response parts are
            classified into the event stream.
- oneshot — the ``run_once`` wrapper opens, sends one turn, drains, closes
            (the per-call fresh session that the convo cleanup uses).

Free-form path only: NO json-schema ``format`` (that forces a
``StructuredOutput`` tool_choice which thinking models — e.g. deepseek-v4 —
reject). For schema'd one-shot output, ``run_once(..., schema=...)`` instructs
the model to emit a bare JSON object and parses it out (see ``api.run_once``).

Leaf module: no awm.config / gateway imports; NO OpenRouter.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any, AsyncIterator, Optional

import httpx

from ._path import resolve_bin
from .session import AgentSession
from .types import AgentConfig, AgentEvent


# opencode Zen's free DeepSeek, overridable via params or env. No OpenRouter.
_DEFAULT_PROVIDER_ID = os.environ.get("AGENTCORE_OPENCODE_PROVIDER", "opencode")
_DEFAULT_MODEL_ID = os.environ.get(
    "AGENTCORE_OPENCODE_MODEL", "deepseek-v4-flash-free"
)

_LISTEN_RE = re.compile(r"https?://(?P<host>[\w.\-]+):(?P<port>\d+)")


def _extract_json_object(text: str) -> Optional[dict]:
    """Pull the first balanced JSON object out of a free-form reply.

    Lifted verbatim (in spirit) from the stt OpencodeAgent so schema'd one-shot
    output survives a thinking-model's plain-text-with-fences reply.
    """
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    try:
        v = json.loads(s)
        if isinstance(v, dict):
            return v
    except ValueError:
        pass
    start = s.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            c = s[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(s[start:i + 1])
                        if isinstance(v, dict):
                            return v
                    except ValueError:
                        break
                    break
        start = s.find("{", start + 1)
    return None


def _classify_parts(parts: list, info: dict) -> list[AgentEvent]:
    """Map an opencode message response (parts + info) → AgentEvents.

    opencode part shapes (verified against the opencode source tree):
      {"type":"text","text": ...}                      → message
      {"type":"reasoning","text": ...}                 → partial (thinking)
      {"type":"tool","tool": ..., "state": {...}}      → tool_use (+tool_result)
      {"type":"step-start"} / {"type":"step-finish"}   → status
    ``info.error`` (if set) → error.
    """
    out: list[AgentEvent] = []
    for p in parts if isinstance(parts, list) else []:
        if not isinstance(p, dict):
            continue
        ptype = p.get("type")
        if ptype == "text":
            text = (p.get("text") or "").strip()
            if text:
                out.append(AgentEvent(kind="message", text=text))
        elif ptype == "reasoning":
            text = (p.get("text") or "").strip()
            if text:
                out.append(AgentEvent(
                    kind="partial", text=text, data={"reasoning": True},
                ))
        elif ptype == "tool":
            name = p.get("tool") or p.get("name") or "?"
            state = p.get("state") or {}
            out.append(AgentEvent(
                kind="tool_use",
                text=f"[tool_use: {name}]",
                data={"name": name, "input": state.get("input")},
            ))
            status = state.get("status")
            output = state.get("output")
            if status in ("completed", "error") or output is not None:
                snippet = str(output or "").strip()
                if len(snippet) > 240:
                    snippet = snippet[:240] + "…"
                out.append(AgentEvent(
                    kind="tool_result",
                    text=snippet or "[tool_result: empty]",
                    data={"name": name, "is_error": status == "error"},
                ))
        elif ptype in ("step-start", "step-finish"):
            out.append(AgentEvent(
                kind="status", text=None, data={"step": ptype},
            ))
    err = (info or {}).get("error")
    if err:
        out.append(AgentEvent(kind="error", text=str(err), data={"error": err}))
    return out


class OpencodeSession(AgentSession):
    """One warm ``opencode serve`` + one persistent session across turns."""

    def __init__(self, config: AgentConfig) -> None:
        super().__init__(config)
        params = config.params or {}
        self.provider_id = params.get("provider_id", _DEFAULT_PROVIDER_ID)
        # An explicit AgentConfig.model wins; else params model_id; else default.
        self.model_id = config.model or params.get("model_id", _DEFAULT_MODEL_ID)
        self.directory = config.workdir or os.getcwd()
        self.request_timeout = float(params.get("request_timeout", 120.0))
        self.startup_timeout = float(params.get("startup_timeout", 30.0))

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._base_url: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._drain_task: Optional[asyncio.Task] = None
        self.session_id: Optional[str] = config.resume_id
        # The event source consumes turn results posted here by send().
        self._turn_q: asyncio.Queue[Optional[list[AgentEvent]]] = asyncio.Queue()

    # ---- server lifecycle ----

    async def _start(self) -> None:
        binary = resolve_bin(self.config.params.get("bin", "opencode")
                             if self.config.params else "opencode")
        argv = [binary, "serve", "--hostname", "127.0.0.1", "--port", "0"]
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._base_url = await self._read_listen_url()
        self._client = httpx.AsyncClient(
            base_url=self._base_url, timeout=self.request_timeout,
        )
        self._drain_task = asyncio.create_task(self._drain_stdout())
        # Open the persistent server-side session if we don't already have one.
        if self.session_id is None:
            self.session_id = await self._open_session()

    async def _read_listen_url(self) -> str:
        assert self._proc is not None and self._proc.stdout is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.startup_timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RuntimeError("opencode serve did not report a listen URL")
            try:
                raw = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=remaining,
                )
            except asyncio.TimeoutError:
                raise RuntimeError("opencode serve startup timed out")
            if not raw:
                code = self._proc.returncode if self._proc else None
                raise RuntimeError(
                    f"opencode serve exited during startup (code {code})"
                )
            line = raw.decode("utf-8", "replace").strip()
            if "listening" in line.lower() or "://" in line:
                m = _LISTEN_RE.search(line)
                if m:
                    host = m.group("host")
                    if host == "0.0.0.0":
                        host = "127.0.0.1"
                    return f"http://{host}:{m.group('port')}"

    async def _drain_stdout(self) -> None:
        if not self._proc or not self._proc.stdout:
            return
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    break
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            pass

    async def _open_session(self) -> str:
        assert self._client is not None
        r = await self._client.post(
            "/session", params={"directory": self.directory}, json={},
        )
        r.raise_for_status()
        return r.json()["id"]

    # ---- turns ----

    async def _post_message(self, text: str) -> dict:
        """POST one free-form (no forced tool_choice) turn; return the JSON."""
        assert self._client is not None and self.session_id is not None
        body: dict[str, Any] = {
            "model": {"providerID": self.provider_id, "modelID": self.model_id},
            "parts": [{"type": "text", "text": text}],
        }
        r = await self._client.post(
            f"/session/{self.session_id}/message",
            params={"directory": self.directory}, json=body,
        )
        r.raise_for_status()
        return r.json() or {}

    async def send(self, text: str) -> None:
        if not self._started:
            await self.start()
        try:
            data = await self._post_message(text)
        except Exception as exc:  # noqa: BLE001
            self._turn_q.put_nowait([
                AgentEvent(kind="error", text=str(exc),
                           data={"exception": type(exc).__name__})
            ])
            return
        parts = data.get("parts") or []
        info = data.get("info") or {}
        events = _classify_parts(parts, info)
        # Synthesize a terminal result so run_once knows the turn is done and
        # downstream consumers see a consistent end-of-turn marker.
        full_text = " ".join(
            e.text for e in events if e.kind == "message" and e.text
        ).strip()
        events.append(AgentEvent(
            kind="result", text=full_text or None,
            data={"session_id": self.session_id},
        ))
        self._turn_q.put_nowait(events)

    async def _event_source(self) -> AsyncIterator[AgentEvent]:
        while True:
            batch = await self._turn_q.get()
            if batch is None:
                return
            for event in batch:
                yield event

    async def close(self) -> None:
        if self._closed:
            return
        # Signal the event source to end.
        self._turn_q.put_nowait(None)
        if self._client is not None and self.session_id is not None:
            try:
                await self._client.delete(
                    f"/session/{self.session_id}",
                    params={"directory": self.directory},
                )
            except Exception:  # noqa: BLE001
                pass
        if self._drain_task is not None:
            self._drain_task.cancel()
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self._proc.kill()
            except ProcessLookupError:
                pass
        await self._stop_pump()

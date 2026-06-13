"""Warm ``opencode serve`` driver for structured LLM completions.

Lifecycle
---------
``OpencodeAgent.start()`` spawns one long-lived ``opencode serve`` subprocess
(``--hostname 127.0.0.1 --port 0`` → kernel-assigned port, parsed from the
startup line). The process stays warm for the life of the agent. Two usage
shapes share that warm server:

- ``complete(prompt, schema)`` — opens a *fresh* session, prompts once, reads
  the structured result, deletes the session. Turns are isolated; continuity
  is the caller's job (it threads its own context/notes into the prompt). This
  is what the convo cleanup loop uses (fresh per silence-cut).
- ``open_session()`` / ``message(id, prompt, schema)`` / ``close_session(id)``
  — hold a *persistent* session across turns, so opencode keeps the turn
  history server-side (maintained context). The mock chatbot uses this.

Wire contract (verified against the opencode source tree)
---------------------------------------------------------
- ``POST /session``                      → ``{"id": ...}`` (create)
- ``POST /session/{id}/message``         body
  ``{"model": {"providerID","modelID"}, "parts":[{"type":"text","text"}],
     "format": {"type":"json_schema","schema": <JSON Schema>}}``
  → ``{"info": {... "structured": <parsed obj>, "error": ...}, "parts":[...]}``
- ``DELETE /session/{id}``               (cleanup)

opencode forces a ``StructuredOutput`` tool call when ``format.type ==
"json_schema"`` and places the validated object at ``info.structured``. If the
model fails to produce it, ``info.error`` is set and ``structured`` is absent;
we surface that as :class:`AgentError` so the caller can fall back.

Auth
----
``opencode serve`` picks up credentials from ``~/.local/share/opencode/auth.json``
automatically, or from ``OPENCODE_API_KEY`` in its environment. This module
does not manage secrets; it only forwards an optional ``api_key`` into the
subprocess env. Wiring the Zen key is a deployment step (see the scope plan).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from typing import Any, Optional

import httpx

log = logging.getLogger("ptt.agent.opencode")


def _extract_json_object(text: str) -> Optional[dict]:
    """Pull the first balanced JSON object out of a free-form reply.

    Thinking models emit their answer as plain text (sometimes fenced, sometimes
    with trailing prose) rather than via a forced tool call. We scan for the
    first ``{...}`` that parses as a dict. Returns ``None`` if there isn't one.
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
                        break  # malformed span; try the next '{'
                    break
        start = s.find("{", start + 1)
    return None

# opencode Zen's free DeepSeek. Both are overridable via env so swapping the
# provider/model (e.g. to ``openrouter`` / a paid tier) is a config change.
DEFAULT_PROVIDER_ID = os.environ.get("CONVO_PROVIDER", "opencode")
DEFAULT_MODEL_ID = os.environ.get("CONVO_MODEL", "deepseek-v4-flash-free")

_LISTEN_RE = re.compile(r"https?://(?P<host>[\w.\-]+):(?P<port>\d+)")


class AgentError(RuntimeError):
    """A completion could not be produced (server down, timeout, or the model
    failed to return structured output). Callers should fall back gracefully."""


class OpencodeAgent:
    """One warm ``opencode serve`` process + structured-completion calls."""

    def __init__(
        self,
        *,
        provider_id: str = DEFAULT_PROVIDER_ID,
        model_id: str = DEFAULT_MODEL_ID,
        bin_path: Optional[str] = None,
        directory: Optional[str] = None,
        api_key: Optional[str] = None,
        request_timeout: float = 60.0,
        startup_timeout: float = 30.0,
    ) -> None:
        self.provider_id = provider_id
        self.model_id = model_id
        self.bin_path = bin_path or os.environ.get("OPENCODE_BIN") or "opencode"
        # Pure cleanup prompts need no project files; default to the service
        # cwd, which opencode also uses as the fallback workspace.
        self.directory = directory or os.getcwd()
        self.api_key = api_key or os.environ.get("OPENCODE_API_KEY")
        self.request_timeout = request_timeout
        self.startup_timeout = startup_timeout

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._base_url: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._drain_task: Optional[asyncio.Task] = None
        self._start_lock = asyncio.Lock()

    # ---- lifecycle ----

    @property
    def base_url(self) -> Optional[str]:
        return self._base_url

    def _resolve_bin(self) -> str:
        found = shutil.which(self.bin_path)
        if not found:
            raise AgentError(
                f"opencode binary {self.bin_path!r} not found on PATH; "
                "install opencode or set OPENCODE_BIN"
            )
        return found

    async def start(self) -> None:
        """Spawn ``opencode serve`` and block until it reports its port."""
        async with self._start_lock:
            if self._is_alive():
                return
            await self._spawn_locked()

    async def _spawn_locked(self) -> None:
        binary = self._resolve_bin()
        env = dict(os.environ)
        if self.api_key:
            env["OPENCODE_API_KEY"] = self.api_key
        argv = [binary, "serve", "--hostname", "127.0.0.1", "--port", "0"]
        log.info("spawning warm opencode serve: %s", " ".join(argv))
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self.directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        self._base_url = await self._read_listen_url()
        self._client = httpx.AsyncClient(
            base_url=self._base_url, timeout=self.request_timeout,
        )
        # Keep draining stdout so a full pipe buffer never wedges the server.
        self._drain_task = asyncio.create_task(self._drain_stdout())
        log.info("opencode serve ready at %s", self._base_url)

    async def _read_listen_url(self) -> str:
        assert self._proc is not None and self._proc.stdout is not None
        deadline = asyncio.get_running_loop().time() + self.startup_timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await self._kill_proc()
                raise AgentError("opencode serve did not report a listen URL in time")
            try:
                raw = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=remaining,
                )
            except asyncio.TimeoutError:
                await self._kill_proc()
                raise AgentError("opencode serve startup timed out")
            if not raw:
                await self._kill_proc()
                code = self._proc.returncode if self._proc else None
                raise AgentError(f"opencode serve exited during startup (code {code})")
            line = raw.decode("utf-8", "replace").strip()
            log.debug("opencode serve: %s", line)
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
                if log.isEnabledFor(logging.DEBUG):
                    log.debug("opencode serve: %s", raw.decode("utf-8", "replace").rstrip())
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.debug("opencode stdout drain ended", exc_info=True)

    def _is_alive(self) -> bool:
        return (
            self._proc is not None
            and self._proc.returncode is None
            and self._base_url is not None
            and self._client is not None
        )

    async def _ensure_running(self) -> None:
        if self._is_alive():
            return
        async with self._start_lock:
            if self._is_alive():
                return
            log.warning("opencode serve not alive; (re)starting")
            await self._cleanup_locked()
            await self._spawn_locked()

    async def _kill_proc(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self._proc.kill()
            except ProcessLookupError:
                pass

    async def _cleanup_locked(self) -> None:
        if self._drain_task:
            self._drain_task.cancel()
            self._drain_task = None
        if self._client:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        await self._kill_proc()
        self._proc = None
        self._base_url = None

    async def stop(self) -> None:
        async with self._start_lock:
            await self._cleanup_locked()

    async def __aenter__(self) -> "OpencodeAgent":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    # ---- session primitives ----
    #
    # The three HTTP steps of a structured completion, exposed separately so a
    # caller can hold a *persistent* session across turns (maintained context)
    # rather than the fresh-session-per-call that ``complete()`` does. All
    # generic — they know nothing about voice or the convo loop.

    async def open_session(self, *, directory: Optional[str] = None) -> str:
        """Create a session and return its id. The caller owns the lifetime —
        nothing is deleted here (see :meth:`close_session`)."""
        await self._ensure_running()
        assert self._client is not None
        params = {"directory": directory or self.directory}
        try:
            r = await self._client.post("/session", params=params, json={})
            r.raise_for_status()
            return r.json()["id"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise AgentError(f"session create failed: {exc}") from exc

    async def message(
        self,
        session_id: str,
        prompt_text: str,
        schema: dict,
        *,
        directory: Optional[str] = None,
    ) -> dict:
        """Send one prompt to an existing session with a json-schema format and
        return the validated structured object from ``info.structured``.

        Raises :class:`AgentError` on any failure (server down, HTTP error, or
        no structured output) so callers can fall back rather than wedge.
        """
        await self._ensure_running()
        assert self._client is not None
        params = {"directory": directory or self.directory}
        body = {
            "model": {"providerID": self.provider_id, "modelID": self.model_id},
            "parts": [{"type": "text", "text": prompt_text}],
            "format": {"type": "json_schema", "schema": schema},
        }
        try:
            r = await self._client.post(
                f"/session/{session_id}/message", params=params, json=body,
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise AgentError(f"prompt failed: {exc}") from exc

        info = (r.json() or {}).get("info") or {}
        structured = info.get("structured")
        if structured is None:
            err = info.get("error")
            raise AgentError(f"no structured output (error={err!r})")
        if not isinstance(structured, dict):
            raise AgentError(f"structured output not an object: {type(structured)}")
        return structured

    async def message_text(
        self,
        session_id: str,
        prompt_text: str,
        *,
        directory: Optional[str] = None,
    ) -> str:
        """Send one prompt to an existing session WITHOUT a json-schema format
        and return the assistant's concatenated ``text`` parts.

        Use for conversational replies: structured output (``message``) forces a
        ``StructuredOutput`` tool call, which *thinking* models (e.g. opencode
        Zen's ``deepseek-v4-flash-free``) reject ("Thinking mode does not support
        this tool_choice"). Free-form text has no such constraint.

        Raises :class:`AgentError` on failure or an empty reply.
        """
        await self._ensure_running()
        assert self._client is not None
        params = {"directory": directory or self.directory}
        body = {
            "model": {"providerID": self.provider_id, "modelID": self.model_id},
            "parts": [{"type": "text", "text": prompt_text}],
        }
        try:
            r = await self._client.post(
                f"/session/{session_id}/message", params=params, json=body,
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise AgentError(f"prompt failed: {exc}") from exc

        data = r.json() or {}
        err = (data.get("info") or {}).get("error")
        if err:
            raise AgentError(f"model error: {err}")
        # The assistant reply is the concatenation of its text parts; reasoning
        # / step-start / tool parts are skipped.
        parts = data.get("parts") or []
        text = " ".join(
            p["text"] for p in parts
            if isinstance(p, dict) and p.get("type") == "text" and p.get("text")
        ).strip()
        if not text:
            raise AgentError("empty reply (no text parts)")
        return text

    async def close_session(
        self, session_id: str, *, directory: Optional[str] = None,
    ) -> None:
        """Best-effort delete of a session. Never raises."""
        if self._client is None:
            return
        params = {"directory": directory or self.directory}
        try:
            await self._client.delete(f"/session/{session_id}", params=params)
        except Exception:  # noqa: BLE001
            log.debug("session %s cleanup failed", session_id, exc_info=True)

    # ---- completion ----

    async def complete(
        self,
        prompt_text: str,
        schema: dict,
        *,
        directory: Optional[str] = None,
    ) -> dict:
        """Open a fresh session, prompt once with a json-schema format, return
        the validated structured object. Deletes the session before returning.

        Fresh-session-per-call keeps turns isolated — there is no cross-turn
        memory; continuity is the caller's job. (For maintained context across
        turns, hold a session via :meth:`open_session` / :meth:`message`.)

        Raises :class:`AgentError` on any failure (server down, HTTP error,
        timeout, or no structured output) so the caller can fall back to raw
        text rather than wedging.
        """
        session_id = await self.open_session(directory=directory)
        try:
            return await self.message(
                session_id, prompt_text, schema, directory=directory,
            )
        finally:
            await self.close_session(session_id, directory=directory)

    async def complete_json(
        self,
        prompt_text: str,
        *,
        directory: Optional[str] = None,
    ) -> dict:
        """Free-form completion that returns a JSON object parsed from the reply.

        Like :meth:`complete` but WITHOUT a json-schema format, so it never
        forces a ``StructuredOutput`` tool_choice. *Thinking* models (opencode
        Zen's ``deepseek-v4-flash-free``) reject a forced tool_choice with a 400
        ("Thinking mode does not support this tool_choice") but happily emit a
        JSON object as their text reply — so this is the model-agnostic path for
        structured-ish output. The prompt MUST instruct the model to return a
        bare JSON object.

        Raises :class:`AgentError` on any failure (server down, HTTP error,
        empty reply, or no parseable JSON object) so the caller can fall back.
        """
        session_id = await self.open_session(directory=directory)
        try:
            text = await self.message_text(
                session_id, prompt_text, directory=directory,
            )
        finally:
            await self.close_session(session_id, directory=directory)
        obj = _extract_json_object(text)
        if obj is None:
            raise AgentError(f"no JSON object in reply: {text[:200]!r}")
        return obj

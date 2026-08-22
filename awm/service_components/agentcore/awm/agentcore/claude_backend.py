"""Claude Code backend — interactive TUI, tmux-driven, transcript-observed.

awm runs ``claude`` as the **interactive** TUI inside a detached ``tmux``
session, so an awm-spawned agent is byte-for-byte the same program a human runs
at the CLI — and a human can ``tmux attach`` to co-drive it. This is the *only*
way a claude agent is spawned (the old headless ``claude --print
--output-format=stream-json`` backend was retired).

An interactive TUI emits no stream-json, so we observe it out-of-band:

- **Input** — ``send(text)`` types a turn into the pane via tmux bracketed paste
  (``load-buffer`` → ``paste-buffer -p`` → ``send-keys Enter``). Bracketed paste
  lands a multi-line prompt as ONE prompt (no early submit at newlines). Slash
  commands (``/compact``, ``/clear``, …) are just pasted text — the real TUI
  runs them natively.
- **Output** — two injected hooks (``--settings``) run :mod:`_hook_emit`, which
  appends to a control file: ``SessionStart`` hands us the on-disk transcript
  JSONL path; each ``Stop`` marks a per-turn boundary. We tail that transcript
  (the transcript shares the stream-json ``type: assistant|user`` +
  ``message.content[]`` envelope, so :func:`_classify` maps it) and synthesize a
  terminal ``result`` on each ``Stop``.

Trust prompt: a fresh worktree triggers claude's "do you trust this folder?"
dialog, which blocks startup (and SessionStart) until dismissed —
``--dangerously-skip-permissions`` does NOT skip it. ``_start`` accepts the
default ("Yes") by sending ``Enter`` once the prompt is detected (or returns
early once SessionStart shows the dir was already trusted).

No token-streaming partials in this mode (whole-message bubbles only).

Leaf module: no awm.config / gateway imports.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from typing import AsyncIterator, List, Optional

from ._mcp import write_claude_mcp_config
from ._path import resolve_bin
from .session import AgentSession
from .types import AgentConfig, AgentEvent


_VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Matches claude's workspace-trust dialog so `_start` can accept the default.
_TRUST_RE = re.compile(r"trust this folder|do you trust", re.IGNORECASE)


def _classify(parsed: dict, cur_msg_id: str | None = None) -> list[AgentEvent]:
    """Map one parsed claude transcript/stream-json object → zero-or-more events.

    Covers the transcript envelope (``assistant`` / ``user`` tool_result /
    ``result`` / ``system`` init). ``stream_event`` partials never appear in the
    tmux transcript but the branch is kept harmless so the classifier stays a
    faithful map of claude's JSON shapes.

    ``cur_msg_id`` (unused by the TUI transcript path) stamps ``message_id`` onto
    partials when present. The terminal ``message`` / ``tool_use`` events carry
    the id read straight off ``parsed['message']['id']`` — the stable per-turn
    key end-to-end.
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


def build_claude_argv(
    config: AgentConfig,
    claude_bin: str,
    session_id: str,
    settings_path: str,
) -> List[str]:
    """Construct the **interactive** ``claude`` argv for the tmux backend.

    Launches the real TUI (no ``--print`` / ``--output-format``).
    ``permissions=='full'`` → ``--dangerously-skip-permissions``; otherwise
    ``--permission-mode default``. Model / effort / system-prompt / tool profile
    / MCP config are threaded in. ``--settings`` injects the SessionStart + Stop
    hooks; a fresh session gets a minted ``--session-id``, a resume uses
    ``--resume``.
    """
    argv = [claude_bin]
    if config.permissions == "full":
        argv.append("--dangerously-skip-permissions")
    else:
        argv.extend(["--permission-mode", "default"])
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
    # MCP is harness-owned: synthesize the awm server (workspace + hub port +
    # AWM_AS) into a spawn-mcp.json, falling back to any pre-built mcp_config.
    mcp_path = write_claude_mcp_config(config) or config.mcp_config
    if mcp_path and os.path.exists(mcp_path):
        argv.extend(["--strict-mcp-config", "--mcp-config", mcp_path])
    argv.extend(["--settings", settings_path])
    if config.resume_id:
        argv.extend(["--resume", config.resume_id])
    else:
        argv.extend(["--session-id", session_id])
    return argv


def _hook_settings(hook_path: str) -> dict:
    """The ``--settings`` doc that injects SessionStart + Stop → _hook_emit.

    Uses ``sys.executable`` (an absolute interpreter that surely exists) over a
    bare ``python3`` so the hook fires regardless of the pane's PATH."""
    cmd = f"{shlex.quote(sys.executable)} {shlex.quote(hook_path)}"
    one = [{"hooks": [{"type": "command", "command": cmd}]}]
    return {"hooks": {"SessionStart": one, "Stop": one}}


def _launcher_script(control_path: str, argv: List[str]) -> str:
    """Bash launcher: export the control path (so the hook subprocess inherits
    it) then ``exec`` the claude TUI."""
    quoted = " ".join(shlex.quote(a) for a in argv)
    return (
        "#!/usr/bin/env bash\n"
        f"export AWM_AGENTCORE_CONTROL_FILE={shlex.quote(control_path)}\n"
        f"exec {quoted}\n"
    )


def _safe_json(line: str) -> Optional[dict]:
    line = line.strip()
    if not line:
        return None
    try:
        v = json.loads(line)
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


class ClaudeSession(AgentSession):
    """One interactive ``claude`` TUI in a detached tmux session.

    Liveness is the tmux session itself (``tmux has-session``), not a child
    process handle — hence the ``wait()`` / ``alive()`` overrides. ``_proc``
    stays ``None`` so the supervisor's pid surfaces read 0 for this harness.
    """

    def __init__(self, config: AgentConfig) -> None:
        super().__init__(config)
        self._proc = None  # no child handle; liveness is the tmux session
        self.session_id: Optional[str] = config.resume_id
        self._tmux_bin: Optional[str] = None
        self._tmux_session: Optional[str] = config.tmux_session_name
        self._pane: Optional[str] = None
        self._tmpdir: Optional[str] = None
        self._control_path: Optional[str] = None

    # ---- lifecycle ----

    async def _start(self) -> None:
        # Resolve binaries up front so a missing tmux/claude is a clear error.
        self._tmux_bin = resolve_bin("tmux")
        claude_bin = resolve_bin("claude")

        if self.config.resume_id:
            self.session_id = self.config.resume_id
        elif self.session_id is None:
            self.session_id = str(uuid.uuid4())
        if not self._tmux_session:
            self._tmux_session = f"awm-{self.session_id[:8]}"

        self._tmpdir = tempfile.mkdtemp(prefix="awm-agentcore-tmux-")
        self._control_path = os.path.join(self._tmpdir, "control.jsonl")
        open(self._control_path, "w").close()  # touch so the tail can open it
        settings_path = os.path.join(self._tmpdir, "settings.json")
        launch_path = os.path.join(self._tmpdir, "launch.sh")
        hook_path = os.path.join(os.path.dirname(__file__), "_hook_emit.py")

        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(_hook_settings(hook_path), f)
        argv = build_claude_argv(
            self.config, claude_bin, self.session_id, settings_path
        )
        with open(launch_path, "w", encoding="utf-8") as f:
            f.write(_launcher_script(self._control_path, argv))
        os.chmod(launch_path, 0o755)

        workdir = self.config.workdir or os.getcwd()
        pane = await self._tmux_out(
            "new-session", "-d", "-s", self._tmux_session, "-c", workdir,
            "-x", "220", "-y", "50", "-P", "-F", "#{pane_id}", launch_path,
        )
        self._pane = pane.strip()
        await self._accept_trust()
        await self._await_ready()

    async def _accept_trust(self) -> None:
        """Dismiss the workspace-trust dialog (default = Yes) if it appears.

        Returns as soon as SessionStart shows up (dir already trusted → no
        prompt) or the dialog is detected and accepted; gives up quietly after
        ~12s so a wording change can't wedge startup."""
        for _ in range(40):
            if self._closed:
                return
            if self._control_has("SessionStart"):
                return  # already trusted — booted straight through
            out = await self._capture_pane()
            if out and _TRUST_RE.search(out):
                await self._tmux("send-keys", "-t", self._pane, "Enter")
                await asyncio.sleep(0.5)
                return
            await asyncio.sleep(0.3)

    async def _await_ready(self) -> None:
        """Block until SessionStart fires (the TUI is past init and the input
        box is focused) before the first ``send`` pastes a prompt.

        Without this the first paste can race the trust-accept → input-ready
        transition and be dropped. SessionStart is read straight off the control
        file (the pump isn't draining yet during ``_start``). Settles briefly
        after, then gives up quietly so a missing hook can't wedge startup."""
        for _ in range(120):  # ~12s
            if self._closed:
                return
            if self._control_has("SessionStart"):
                await asyncio.sleep(0.8)
                return
            await asyncio.sleep(0.1)

    async def send(self, text: str) -> None:
        if not self._started:
            await self.start()
        if self._closed or not self._pane:
            raise RuntimeError("tmux claude session not available")
        await self._paste_prompt(text)

    async def interrupt(self, text: str) -> None:
        """Forced-interrupt notification: ESC to cancel claude's in-flight turn,
        then paste ``text`` as a fresh prompt. ESC in the claude TUI aborts a
        running generation; a short settle beat lets the input box refocus
        before the paste so the notification isn't swallowed mid-cancel."""
        if not self._started:
            await self.start()
        if self._closed or not self._pane:
            raise RuntimeError("tmux claude session not available")
        await self._tmux("send-keys", "-t", self._pane, "Escape")
        await asyncio.sleep(0.15)
        await self._paste_prompt(text)

    async def _paste_prompt(self, text: str) -> None:
        # Bracketed paste: a multi-line body lands as ONE prompt (no submit at
        # newlines), then an explicit Enter submits it.
        buf = f"awm{uuid.uuid4().hex[:8]}"
        proc = await asyncio.create_subprocess_exec(
            self._tmux_bin, "load-buffer", "-b", buf, "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate(text.encode("utf-8"))
        await self._tmux("paste-buffer", "-d", "-p", "-b", buf, "-t", self._pane)
        await asyncio.sleep(0.2)
        await self._tmux("send-keys", "-t", self._pane, "Enter")

    async def _event_source(self) -> AsyncIterator[AgentEvent]:
        assert self._control_path is not None
        cf = open(self._control_path, "r", encoding="utf-8")
        cbuf = ""
        transcript_path: Optional[str] = None

        # Phase 1: tail the control file until SessionStart hands us the path.
        while transcript_path is None and not self._closed:
            chunk = cf.read()
            if chunk:
                cbuf += chunk
                *lines, cbuf = cbuf.split("\n")
                for ln in lines:
                    rec = _safe_json(ln)
                    if rec and rec.get("event") == "SessionStart" \
                            and rec.get("transcript_path"):
                        transcript_path = rec["transcript_path"]
                        if rec.get("session_id"):
                            self.session_id = rec["session_id"]
                        break
            if transcript_path is None:
                await asyncio.sleep(0.1)
        if transcript_path is None:
            cf.close()
            return
        yield AgentEvent(
            kind="status",
            data={"subtype": "session_start",
                  "transcript_path": transcript_path,
                  "session_id": self.session_id},
        )

        # Open the transcript. claude creates it lazily — for an idle
        # conversational agent it may not appear until the FIRST turn, an
        # unbounded wait after SessionStart. So poll until it exists or the
        # session closes (a fixed cap would strand an agent that sits on the
        # welcome screen before its first message).
        tf = None
        while not self._closed:
            if os.path.exists(transcript_path):
                tf = open(transcript_path, "r", encoding="utf-8")
                break
            await asyncio.sleep(0.2)
        if tf is None:
            cf.close()
            return
        if self.config.resume_id:
            tf.seek(0, os.SEEK_END)  # skip prior-turn history on resume

        tbuf = ""
        last_msg_text: Optional[str] = None
        try:
            while not self._closed:
                progressed = False
                # Drain new complete transcript lines → events.
                chunk = tf.read()
                if chunk:
                    progressed = True
                    tbuf += chunk
                    *lines, tbuf = tbuf.split("\n")
                    for ln in lines:
                        for ev in self._classify_line(ln):
                            if ev.kind == "message" and ev.text:
                                last_msg_text = ev.text
                            yield ev
                # Watch the control file for a Stop (per-turn boundary).
                cchunk = cf.read()
                stop = False
                if cchunk:
                    progressed = True
                    cbuf += cchunk
                    *clines, cbuf = cbuf.split("\n")
                    for ln in clines:
                        rec = _safe_json(ln)
                        if rec and rec.get("event") == "Stop":
                            stop = True
                if stop:
                    # The assistant transcript line can LAG the Stop hook (the
                    # two are separate files with independent flush timing —
                    # pronounced when claude responds fast). Retry-drain until
                    # the transcript settles (a short quiet gap) or a cap, so the
                    # turn's message isn't lost ahead of the synthesized result.
                    quiet = 0
                    for _ in range(40):  # up to ~4s
                        chunk = tf.read()
                        if chunk:
                            tbuf += chunk
                            *lines, tbuf = tbuf.split("\n")
                            for ln in lines:
                                for ev in self._classify_line(ln):
                                    if ev.kind == "message" and ev.text:
                                        last_msg_text = ev.text
                                    yield ev
                            quiet = 0
                        else:
                            quiet += 1
                            if quiet >= 3:  # ~0.3s with no new data → settled
                                break
                            await asyncio.sleep(0.1)
                    yield AgentEvent(
                        kind="result", text=last_msg_text,
                        data={"session_id": self.session_id,
                              "turn_boundary": True},
                    )
                    last_msg_text = None
                if not progressed:
                    await asyncio.sleep(0.1)
        finally:
            try:
                tf.close()
            finally:
                cf.close()

    def _classify_line(self, line: str) -> List[AgentEvent]:
        """One transcript JSONL line → events, with transcript-stable ids.

        The transcript shares the stream-json ``type``/``message.content[]``
        envelope, so :func:`_classify` maps it. Unknown transcript entry types
        (``mode``, ``attachment``, ``system``, …) classify to nothing. A plain
        user-text entry has a string ``content`` → no event (we don't echo the
        prompt). The per-line ``uuid`` keys a stable event id so a ws
        backfill/live overlap dedupes."""
        rec = _safe_json(line)
        if rec is None:
            return []
        events = _classify(rec)
        uid = rec.get("uuid")
        if uid:
            for i, ev in enumerate(events):
                ev.id = f"{uid}#{i}"
        return events

    # ---- liveness (tmux session, not a child proc) ----

    def alive(self) -> bool:
        if self._closed or not self._tmux_session or not self._tmux_bin:
            return False
        r = subprocess.run(
            [self._tmux_bin, "has-session", "-t", self._tmux_session],
            capture_output=True,
        )
        return r.returncode == 0

    async def wait(self) -> Optional[int]:
        """Block until the tmux session ends (crash / external kill / close)."""
        while not self._closed and self.alive():
            await asyncio.sleep(1.0)
        return None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True  # set first so the tail + wait loops exit
        if self._tmux_session and self._tmux_bin:
            try:
                await self._tmux("kill-session", "-t", self._tmux_session)
            except Exception:  # noqa: BLE001 — teardown must never raise
                pass
        await self._stop_pump()
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ---- tmux helpers ----

    async def _tmux(self, *args: str) -> int:
        proc = await asyncio.create_subprocess_exec(
            self._tmux_bin, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode if proc.returncode is not None else -1

    async def _tmux_out(self, *args: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            self._tmux_bin, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"tmux {' '.join(args)} failed: "
                f"{err.decode('utf-8', 'replace').strip()}"
            )
        return out.decode("utf-8", "replace")

    async def _capture_pane(self) -> str:
        if not self._pane:
            return ""
        try:
            return await self._tmux_out("capture-pane", "-t", self._pane, "-p")
        except Exception:  # noqa: BLE001
            return ""

    def _control_has(self, event: str) -> bool:
        path = self._control_path
        if not path or not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                return any(
                    (_safe_json(ln) or {}).get("event") == event for ln in f
                )
        except OSError:
            return False

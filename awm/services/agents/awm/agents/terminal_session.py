"""Interactive terminal direct-session: relay a ``tmux attach`` PTY to a browser.

A ``claude`` agent runs the real ``claude`` TUI inside a *detached* tmux
session (``awm-<instance_id>-<scope>``, recorded as the instance's
``tmux_session``). This session handler attaches a PTY-backed ``tmux attach``
client to that session and byte-relays it over the hub bridge, so a browser
xterm.js can both **view** the live terminal and **drive** it (keystrokes).

Detach — never kill — on close: the agent's tmux session (and the ``claude``
inside it) outlives any number of viewers. The agent is pure I/O text; the
*connected UI dictates the size* (we apply the client's resize to the PTY and
let tmux repaint to it).

Wire protocol over the raw ``direct`` bridge (``SessionContext.open_bridge()``):

  server → browser
    binary frame  →  raw terminal output bytes (a PTY-master read)
    text frame    →  JSON control, e.g. ``{"type": "error", "message": ...}``

  browser → server
    binary frame  →  keystroke bytes (written verbatim to the PTY master)
    text frame    →  JSON control: ``{"type": "resize", "cols": C, "rows": R}``
                     (a non-control / unparseable text frame is treated as
                     keystrokes, so a plain-text client still works)

Init (``ctx.init``): ``{scope}`` or ``{session_id}`` to resolve the live agent,
plus optional ``{cols, rows}`` for the initial PTY size.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pty
import struct
import subprocess
import termios
from typing import Optional, Tuple

from awm.gatewayclient.adapter import SessionContext
from awm.agents import agent_instances as ai

log = logging.getLogger(__name__)

# Fallback PTY geometry when the client doesn't send one in init; the browser
# sends a `resize` control frame right after open, so this is only transient.
_DEFAULT_COLS = 120
_DEFAULT_ROWS = 32
_READ_CHUNK = 65536


def _tmux_has_session(name: str) -> bool:
    """True if a tmux session called ``name`` currently exists."""
    try:
        return subprocess.run(
            ["tmux", "has-session", "-t", f"={name}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3,
        ).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _resolve_tmux_session(init: dict) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the terminal target for ``init`` → a tmux session name.

    Three identity forms, in priority order:

    - ``{tmux_session}`` — an **ad-hoc** attach by name (any machine-wide tmux
      session the fleet roster surfaced, not necessarily an agents-registry
      agent). Verified to exist; registry-agnostic.
    - ``{session_id}`` / ``{scope}`` — resolve a live *registry* agent and use
      its recorded ``tmux_session``.

    Returns ``(tmux_session, None)`` on success or ``(None, error_message)``
    when the identity is missing, the session/agent doesn't exist, or the agent
    is not a claude agent (so it has no tmux pane to attach — e.g. opencode)."""
    name = init.get("tmux_session")
    if name:
        if not _tmux_has_session(str(name)):
            return None, f"no tmux session named {name!r}"
        return str(name), None
    sid = init.get("session_id")
    if sid is not None:
        try:
            inst = ai.get_session(int(sid))
        except (TypeError, ValueError):
            return None, f"invalid session_id {sid!r}"
    else:
        scope = init.get("scope")
        if not scope:
            return None, "init requires {scope} or {session_id}"
        inst = ai.get_session_by_scope(scope)
    if inst is None:
        return None, "no live agent session for that identity"
    tmux = getattr(inst, "tmux_session", None)
    if not tmux:
        return None, ("agent has no tmux session to attach "
                      "(not a claude agent)")
    return tmux, None


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    """Set the PTY window size; the kernel sends SIGWINCH to the attached
    ``tmux`` client, which repaints the session to the new geometry."""
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


async def terminal_session(ctx: SessionContext) -> None:
    """Drive one interactive ``tmux attach`` PTY relay over a direct session."""
    init = ctx.init or {}
    tmux_session, err = _resolve_tmux_session(init)

    bridge = await ctx.open_bridge()
    if err:
        try:
            await bridge.send(json.dumps({"type": "error", "message": err}))
        except Exception:  # noqa: BLE001
            pass
        try:
            await bridge.close()
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        cols = int(init.get("cols") or _DEFAULT_COLS)
        rows = int(init.get("rows") or _DEFAULT_ROWS)
    except (TypeError, ValueError):
        cols, rows = _DEFAULT_COLS, _DEFAULT_ROWS

    # Allocate a PTY and spawn `tmux attach` bound to its slave. tmux REQUIRES a
    # real controlling terminal — pipes error "open terminal failed: not a
    # terminal" — so the slave fd is the child's stdin/stdout/stderr and
    # start_new_session makes it the child's controlling tty.
    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, cols, rows)
    env = dict(os.environ, TERM="xterm-256color")
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "-u", "attach-session", "-t", tmux_session,
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            start_new_session=True, env=env,
        )
    except Exception as exc:  # noqa: BLE001
        os.close(master_fd)
        os.close(slave_fd)
        try:
            await bridge.send(json.dumps(
                {"type": "error", "message": f"tmux attach failed: {exc}"}))
            await bridge.close()
        except Exception:  # noqa: BLE001
            pass
        return
    os.close(slave_fd)  # the child owns the slave now

    loop = asyncio.get_running_loop()
    out_q: asyncio.Queue = asyncio.Queue(maxsize=1024)

    def _on_master_readable() -> None:
        # add_reader callback (sync): read the PTY master and hand bytes to the
        # ordered drain task. EOF (child detached/exited) → None sentinel.
        try:
            data = os.read(master_fd, _READ_CHUNK)
        except OSError:
            data = b""
        if not data:
            try:
                loop.remove_reader(master_fd)
            except Exception:  # noqa: BLE001
                pass
            try:
                out_q.put_nowait(None)
            except asyncio.QueueFull:
                pass
            return
        try:
            out_q.put_nowait(data)
        except asyncio.QueueFull:
            # Terminal output backpressure (a flood faster than the socket can
            # drain). Drop this chunk rather than stall the event loop; xterm
            # tolerates gaps and the next full repaint reconciles.
            pass

    loop.add_reader(master_fd, _on_master_readable)

    async def drain_out() -> None:
        while True:
            data = await out_q.get()
            if data is None:
                break
            try:
                await bridge.send(data)
            except Exception:  # noqa: BLE001
                break

    async def pump_in() -> None:
        while True:
            try:
                frame = await bridge.recv()
            except Exception:  # noqa: BLE001
                break
            if isinstance(frame, (bytes, bytearray)):
                try:
                    os.write(master_fd, frame)
                except OSError:
                    break
                continue
            # Text frame → JSON control (or, defensively, raw keystrokes).
            try:
                msg = json.loads(frame)
            except (ValueError, TypeError):
                try:
                    os.write(master_fd, frame.encode("utf-8", "ignore"))
                except OSError:
                    break
                continue
            if isinstance(msg, dict) and msg.get("type") == "resize":
                try:
                    c = int(msg.get("cols") or cols)
                    r = int(msg.get("rows") or rows)
                except (TypeError, ValueError):
                    continue
                _set_winsize(master_fd, c, r)
            elif isinstance(msg, dict) and msg.get("type") == "input":
                # Optional explicit keystroke envelope for text-only clients.
                try:
                    os.write(master_fd, str(msg.get("data", "")).encode("utf-8"))
                except OSError:
                    break

    drainer = asyncio.create_task(drain_out())
    pumper = asyncio.create_task(pump_in())
    try:
        # Either direction ending (socket closed, or PTY EOF) tears the pair down.
        await asyncio.wait({drainer, pumper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        try:
            loop.remove_reader(master_fd)
        except Exception:  # noqa: BLE001
            pass
        for t in (drainer, pumper):
            if not t.done():
                t.cancel()
        # Detach (NOT kill) the tmux client: terminate the `tmux attach` child.
        # The agent's session and its `claude` keep running.
        if proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            await bridge.close()
        except Exception:  # noqa: BLE001
            pass

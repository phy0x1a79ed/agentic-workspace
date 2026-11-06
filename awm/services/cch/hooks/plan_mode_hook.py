#!/usr/bin/env python3
"""Claude Code hook: put a session back into bypass-permissions mode on plan approval.

Approving a plan does not restore the mode the session was launched with — it
restores whatever mode it held immediately *before* planning, which for a plan
approved from a phone is ``auto``: a classifier gates every action, and the first
thing that happens after approval is friction. The service already ships the fix
(``reflection_mode`` walks the Shift+Tab cycle on the calling session); this is
the trigger it was written for.

Installed as an *additional* ``PostToolUse`` entry matching ``ExitPlanMode``, so
it composes with whatever else already matches that tool. It emits no permission
decision and writes nothing to stdout — a ``PostToolUse`` hook's stdout is parsed
as a JSON control object, so a stray print would be read as one.

Two guards decide whether to call at all, and both read the payload Claude Code
hands us:

* ``tool_response`` must be a dict carrying a ``plan`` key. That is the shape an
  *approved* plan returns. A rejection does not reach a PostToolUse hook at all
  (it is a separate ``PermissionDenied`` event), so this is a guard against the
  harness changing under us rather than a live case — but cycling the mode after
  a rejection would kick the session out of plan mode, which is worse than doing
  nothing, so it is worth the two lines.
* ``permission_mode`` is the mode ``ExitPlanMode`` has already restored by the
  time we run, never ``"plan"``. Already-bypass needs no call; a literal
  ``"plan"`` means something unmodelled happened and we stand down.

An unreadable footer is retried on either lane: the service renders a
background session's stream into a screen, so "unknown" there means a modal
covering the footer, as it does on tmux, and whatever covers it right after an
approval is transient.

A call that times out is never retried. The walk it started is still pressing
Shift+Tab on the server, and a second walk interleaved with it steps past bypass
— observed live, when an 8-second timeout fired on a busy session's 17-second
walk. Only a gateway that could not be reached at all, where nothing started, is
retried.

Identity is the reason this is not a one-line ``curl``. Reflection acts only on
the calling session and works that out by observing the caller's pid — but a
hook's own pid names no session, so the gateway walks its ancestry
(``X-Awm-Caller-Pid``). A walk can climb past a session whose record is missing
and land on whoever *launched* it, so the ``session_id`` Claude Code hands us on
stdin is sent along as ``expect_session`` and reflection refuses on a mismatch.
It can only ever refuse, never redirect.

This runs **synchronously**. Detaching (as the notify hook does) would reparent
us away from the REPL and destroy the very ancestry the walk depends on, and the
stall is worth having: it means the mode is right before the next tool call runs.

Pure stdlib, runs under ``python3 -S``, exits 0 on every path.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

TARGET_TOOL = "ExitPlanMode"
LOG_PATH = os.path.expanduser("~/.claude/awm-reflection-mode.log")

# The TUI is still repainting the moment the tool returns, and the mode is read
# off that paint. A beat here is cheaper than a retry later.
SETTLE_S = 0.6

# Bounded by construction: the settings entry gives this hook a timeout, and
# overrunning it means Claude Code kills us mid-POST. Everything below has to
# fit inside that with room to spare.
HTTP_TIMEOUT_S = 15.0
DEADLINE_S = 17.0
RETRY_GAP_S = 1.5
MIN_ATTEMPT_S = 3.0


def hub_url() -> str:
    return (os.environ.get("AWM_HUB_URL") or "http://127.0.0.1:7819").rstrip("/")


def note(message: str) -> None:
    """Append one line to the log. This is the whole answer to 'it silently
    didn't happen' — the retry covers a gateway restart, this covers the rest."""
    try:
        with open(LOG_PATH, "a") as fh:
            fh.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


def call_mode(session_id: str, timeout: float = HTTP_TIMEOUT_S) -> dict:
    """One ``reflection_mode`` call over plain HTTP. Raises on transport failure.

    The flat ``/invoke`` surface rather than the CLI: the generated CLI sends no
    headers, so the gateway would strip ``_caller_pid`` and the call would refuse.
    """
    body = json.dumps({
        "name": "reflection_mode",
        "args": {"expect_session": session_id},
    }).encode()
    req = urllib.request.Request(
        hub_url() + "/invoke", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "X-Awm-Caller-Pid": str(os.getpid())})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode() or "{}")
    return payload.get("result") or {}


def should_act(payload: dict) -> bool:
    if payload.get("tool_name") != TARGET_TOOL:
        return False
    response = payload.get("tool_response")
    if not isinstance(response, dict) or "plan" not in response:
        return False
    return payload.get("permission_mode") not in ("bypassPermissions", "plan")


def timed_out(exc: BaseException) -> bool:
    return isinstance(exc, TimeoutError) or isinstance(
        getattr(exc, "reason", None), TimeoutError)


def ensure_bypass(session_id: str) -> None:
    deadline = time.monotonic() + DEADLINE_S
    last = ""
    while True:
        budget = min(HTTP_TIMEOUT_S, deadline - time.monotonic())
        try:
            result = call_mode(session_id, timeout=budget)
        except urllib.error.HTTPError as exc:
            last = "HTTP %s from the gateway" % exc.code
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if timed_out(exc):
                note("session %s: the mode call timed out after %.0fs and was "
                     "not retried, because its walk may still be running"
                     % (session_id, budget))
                return
            # Connection refused is the gateway mid-restart, which is exactly
            # what the retry window is for.
            last = "could not reach the gateway: %s" % exc
        else:
            if result.get("ok"):
                return
            last = str(result.get("error") or result.get("reason") or result)
            if result.get("mode") != "unknown":
                break
        if deadline - time.monotonic() < RETRY_GAP_S + MIN_ATTEMPT_S:
            break
        time.sleep(RETRY_GAP_S)
    note("session %s not switched to bypass: %s" % (session_id, last))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 — malformed input is not our problem
        return 0

    session_id = payload.get("session_id") or ""
    if not session_id or not should_act(payload):
        return 0

    time.sleep(SETTLE_S)
    ensure_bypass(session_id)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — never break a turn
        sys.exit(0)

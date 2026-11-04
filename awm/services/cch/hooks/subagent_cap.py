#!/usr/bin/env python3
"""The awm subagent cap: a session below trusted runs at most 5 subagents at a time.

`channels.md` R125 item 7. One script serves three Claude Code hook events.
SubagentStart and SubagentStop keep each session's running subagents by
agent id. PreToolUse on the Agent tool reserves a slot, and refuses the call
when the running and reserved subagents of a cooperative or untrusted session
already fill the cap.

A reservation covers the gap between PreToolUse and SubagentStart, so Agent
calls sent together in one turn count each other. SubagentStart turns the
oldest reservation into a running subagent. A reservation whose subagent never
starts, because another hook refused the call, lapses after RESERVE_S. A
running subagent whose SubagentStop never comes, because the session was
killed, lapses after RUNNING_S, so a lost stop frees its slot in the end.

The level comes from `reflection whoami`, asked only when the cap is full. A
lookup that fails counts as untrusted (fleet.md R103 item 2). Every path exits 0.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.request

CAP = 5
RESERVE_S = 60.0
RUNNING_S = 4 * 3600.0
TIMEOUT_S = 3.0
AGENT_TOOLS = ("Agent", "Task")


def state_dir() -> str:
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(state, "awm", "cch", "subagents")


def level() -> str:
    """The calling session's trust level, or untrusted when reflection cannot say."""
    url = (os.environ.get("AWM_HUB_URL") or "http://127.0.0.1:7819").rstrip("/") + "/invoke"
    body = json.dumps({"name": "reflection_whoami", "args": {}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Awm-Caller-Pid": str(os.getppid())})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            result = json.loads(resp.read().decode() or "{}").get("result")
        who = json.loads(result) if isinstance(result, str) else result
    except (urllib.error.URLError, OSError, ValueError):
        return "untrusted"
    trust = who.get("trust") if isinstance(who, dict) else None
    return trust if trust in ("trusted", "cooperative", "untrusted") else "untrusted"


def _update(session_id: str, change) -> dict:
    """Apply ``change`` to the session's ``{running: {agent_id: started}, reserved}`` under a
    lock, and save it."""
    folder = state_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{session_id}.json")
    with open(path, "a+") as fp:
        fcntl.flock(fp, fcntl.LOCK_EX)
        fp.seek(0)
        try:
            state = json.loads(fp.read() or "{}")
        except ValueError:
            state = {}
        now = time.time()
        running = state.get("running")
        state = {"running": {a: t for a, t in running.items() if now - t < RUNNING_S}
                 if isinstance(running, dict) else {},
                 "reserved": sorted(t for t in state.get("reserved") or [] if now - t < RESERVE_S)}
        out = change(state, now)
        fp.seek(0)
        fp.truncate()
        fp.write(json.dumps(state))
        return out


def started(agent_id: str | None):
    def change(state: dict, now: float) -> None:
        state["running"][agent_id or f"anon-{os.urandom(4).hex()}"] = now
        if state["reserved"]:
            state["reserved"].pop(0)
    return change


def stopped(agent_id: str | None):
    """A stop with no agent id stands for the oldest running subagent."""
    def change(state: dict, now: float) -> None:
        running = state["running"]
        if agent_id is None and running:
            running.pop(min(running, key=running.get))
        else:
            running.pop(agent_id, None)
    return change


def reserve(state: dict, now: float) -> str | None:
    """Reserve a slot, or answer why not."""
    busy = len(state["running"]) + len(state["reserved"])
    if busy >= CAP:
        lv = level()
        if lv != "trusted":
            return (f"A {lv} session runs at most {CAP} subagents at a time, and {busy} are "
                    "running or starting. Wait for one to finish (channels.md R125 item 7).")
    state["reserved"].append(now)
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return
    if not isinstance(payload, dict):
        return
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id or "/" in session_id:
        return
    event = payload.get("hook_event_name")
    agent_id = payload.get("agent_id") if isinstance(payload.get("agent_id"), str) else None
    try:
        if event == "SubagentStart":
            _update(session_id, started(agent_id))
        elif event == "SubagentStop":
            _update(session_id, stopped(agent_id))
        elif event == "PreToolUse" and payload.get("tool_name") in AGENT_TOOLS:
            why = _update(session_id, reserve)
            if why:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": why}}))
    except OSError:
        return


if __name__ == "__main__":
    main()

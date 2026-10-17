#!/usr/bin/env python3
"""Claude Code hook: report a session's plan, todos and prompts to the local `reporting` service.

A SessionStart on source ``clear`` tells the service to file the session's task away,
so the next plan opens a new card. ``compact`` and ``resume`` also fire SessionStart
and are ignored.

Pure stdlib, runs under ``python3 -S`` and exits 0 on every path. It writes to stdout
only when a card opened: the service's ``notice``, as context the agent reads. A
UserPromptSubmit hook's stdout becomes model context, so a prompt prints nothing else.

The call is synchronous ``POST /invoke`` with ``X-Awm-Caller-Pid``: the gateway walks
the hook's ancestry to find the session, which a detached hook would break. The
``session_id`` rides along only as a check.

Each verb result carries ``blocked``. It is mirrored into a marker file so that
UserPromptSubmit, which runs on every prompt, makes no network call unless the
prompt holds a Trilium note URL or the session's task is Blocked. A background
job's first prompt is the exception: it asks once whether the job is a briefed
worker, whose card opens then, and a ``.seen`` marker records that it asked.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
PLANS_DIR = os.path.join(HOME, ".claude", "plans")
MARKER_DIR = os.path.join(HOME, ".claude", "awm-reporting")
# The same note URL and claim words the service parses. The vault sits at /trilium/ on
# a fleet edge and at the root on nexus, so the path names no product.
NOTE_URL = re.compile(r"https?://[^\s#]+#(?:[\w-]+/)*[\w-]{4,}")
CLAIM_WORDS = re.compile(r"\b(claim|attach)\b", re.I)

# Fits inside the settings entry's timeout, or Claude Code kills us mid-POST.
DEADLINE_S = 8.0
RETRY_GAP_S = 1.0


def call(verb: str, **args) -> dict:
    """Retry only refused connections: that is the gateway restarting."""
    url = (os.environ.get("AWM_HUB_URL") or "http://127.0.0.1:7819").rstrip("/") + "/invoke"
    body = json.dumps({"name": "operations_" + verb, "args": args}).encode()
    headers = {"Content-Type": "application/json", "X-Awm-Caller-Pid": str(os.getpid())}
    deadline = time.monotonic() + DEADLINE_S
    while True:
        try:
            req = urllib.request.Request(url, data=body, method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=max(1.0, deadline - time.monotonic())) as resp:
                result = json.loads(resp.read().decode() or "{}").get("result")
            if isinstance(result, str):  # /invoke carries the verb's result as JSON text
                result = json.loads(result)
            return result if isinstance(result, dict) else {}
        except urllib.error.HTTPError:
            return {}
        except (urllib.error.URLError, OSError, ValueError):
            if time.monotonic() + RETRY_GAP_S >= deadline:
                return {}
            time.sleep(RETRY_GAP_S)


def marker(session_id: str, kind: str = "blocked") -> str:
    return os.path.join(MARKER_DIR, session_id.replace("/", "_") + "." + kind)


def report(session_id: str, verb: str, **args) -> dict:
    result = call(verb, session_id=session_id, **args)
    if not isinstance(result.get("blocked"), bool):
        return result
    try:
        if result["blocked"]:
            touch(marker(session_id))
        else:
            os.unlink(marker(session_id))
    except OSError:
        pass
    return result


def touch(path: str) -> None:
    try:
        os.makedirs(MARKER_DIR, exist_ok=True)
        open(path, "w").close()
    except OSError:
        pass


def tell(result: dict, event: str = "PostToolUse") -> None:
    notice = result.get("notice")
    if isinstance(notice, str) and notice:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                                 "additionalContext": notice}}))


def read_text(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


def task_items(session_id: str) -> list[dict]:
    """The full list lives on disk: TaskCreate and TaskUpdate payloads are deltas."""
    list_id = os.environ.get("CLAUDE_CODE_TASK_LIST_ID") or session_id
    items = []
    for path in glob.glob(os.path.join(HOME, ".claude", "tasks", list_id, "*.json")):
        try:
            task = json.loads(read_text(path))
            items.append({"id": str(task["id"]), "subject": task["subject"], "status": task["status"]})
        except (ValueError, KeyError, TypeError):
            continue
    return sorted(items, key=lambda i: (len(i["id"]), i["id"]))


def todowrite_items(todos) -> list[dict]:
    return [{"id": str(t.get("id") or n), "subject": t.get("content", ""), "status": t.get("status", "")}
            for n, t in enumerate(todos or [], 1) if isinstance(t, dict)]


def handle(p: dict) -> None:
    sid = p.get("session_id") or ""
    if not sid:
        return
    tool = p.get("tool_name")
    tool_input = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}

    if p.get("hook_event_name") == "SessionStart":
        if p.get("source") == "clear":
            call("hook_session_clear")
    elif p.get("hook_event_name") == "UserPromptSubmit":
        prompt = p.get("prompt") or ""
        claims = CLAIM_WORDS.search(prompt) and NOTE_URL.search(prompt)
        first = bool(os.environ.get("CLAUDE_JOB_DIR")) and not os.path.exists(marker(sid, "seen"))
        if first:
            result = report(sid, "hook_prompt", prompt=prompt, first=True)
            if result:
                touch(marker(sid, "seen"))
            tell(result, "UserPromptSubmit")
        elif claims or os.path.exists(marker(sid)):
            report(sid, "hook_prompt", prompt=prompt)
    elif tool in ("Write", "Edit"):
        path = os.path.abspath(str(tool_input.get("file_path") or ""))
        if os.path.dirname(path) == PLANS_DIR and path.endswith(".md") and not p.get("agent_id"):
            tell(report(sid, "hook_plan", plan_path=path, plan_text=read_text(path)))
    elif tool == "ExitPlanMode" and not p.get("agent_id"):
        response = p.get("tool_response")
        if isinstance(response, dict) and response.get("plan"):
            tell(report(sid, "hook_plan_approved", plan_text=response["plan"]))
    elif tool in ("TaskCreate", "TaskUpdate", "TodoWrite"):
        items = todowrite_items(tool_input.get("todos")) if tool == "TodoWrite" else task_items(sid)
        # An empty list is a wipe only when the event is a deletion; a cleared list is not.
        if items or tool_input.get("status") == "deleted":
            report(sid, "hook_todos", items=items)


if __name__ == "__main__":
    try:
        handle(json.load(sys.stdin))
    except Exception:  # noqa: BLE001 — never break a turn
        pass
    sys.exit(0)

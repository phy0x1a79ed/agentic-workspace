#!/usr/bin/env python3
"""Claude Code hook: deliver compute-watchdog notices, and warn before launch.

Installed as *additional* entries in the existing ``PreToolUse`` and
``PostToolUse`` arrays in ``~/.claude/settings.json`` — it composes with the
``tmux kill-server`` deny and the claude-view telemetry hooks rather than
replacing them. It emits no permission decision, so it cannot override that
deny.

Two jobs, chosen by ``hook_event_name``:

* **PostToolUse** — drain this session's notices. The agent is almost always
  blocked on the very command that was stopped, so this is where a bare
  ``Killed`` gets explained.
* **PreToolUse** — drain the same directory (for a *detached* job nobody was
  waiting on, this is the first opportunity), then, if the box is loaded, add a
  one-line heads-up before the agent launches more work.

Design constraints, all of them non-negotiable for something that runs on every
tool call:

* **Standard library only.** No awm imports, no third-party packages — this
  runs under whatever ``python3`` is on PATH, in any session on this machine.
  The notice format is duplicated from ``awm.compute.notices`` rather than
  imported; that is deliberate.
* **No network.** One directory listing and a few small file reads.
* **Fail open, always.** Every path is wrapped; any error exits 0 silently. A
  watchdog that breaks tool calls is worse than no watchdog.
* **Sub-millisecond in the common case** — an empty directory listing.
* Paths resolve from ``$HOME`` (or ``AWM_COMPUTE_NOTICE_DIR``), never from
  ``AWM_WORKSPACE``, which points at a worktree for sandbox-spawned agents.
"""

from __future__ import annotations

import json
import os
import sys
import time

MAX_NOTICES = 5
BOX_MAX_AGE_S = 30.0


def notice_root() -> str:
    override = os.environ.get("AWM_COMPUTE_NOTICE_DIR")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".claude", "compute-notices")


def _safe(session_id: str) -> str:
    return "".join(
        c if (c.isalnum() or c in "_.-") else "_" for c in session_id
    )[:128]


def drain(session_id: str) -> list[dict]:
    d = os.path.join(notice_root(), _safe(session_id))
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        return []
    out = []
    for name in names[:MAX_NOTICES]:
        path = os.path.join(d, name)
        try:
            with open(path) as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            pass
        try:
            os.unlink(path)
        except OSError:
            pass
    return out


def read_box() -> dict | None:
    try:
        with open(os.path.join(notice_root(), "_box.json")) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    # A stale snapshot means the watchdog is down. Say nothing rather than
    # quoting numbers from an hour ago.
    if time.time() - float(data.get("wall_ts", 0)) > BOX_MAX_AGE_S:
        return None
    return data


def render(n: dict) -> str:
    action = n.get("action", "acted on")
    kind = n.get("kind", "")
    lines = []
    if action == "terminated":
        lines.append(
            "⛔ A local job started by this session was STOPPED by the awm "
            "compute watchdog."
        )
    elif action == "deprioritized":
        lines.append(
            "\U0001f40c A local job started by this session was DEPRIORITIZED "
            "(renice %s) by the awm compute watchdog. It was NOT killed and "
            "will still finish; it now yields to interactive work."
            % n.get("nice", 19)
        )
    else:
        lines.append("awm compute watchdog: %s." % action)

    lines.append("  command: %s" % (n.get("cmdline") or "")[:300])
    if n.get("metric") == "memory":
        lines.append(
            "  memory:  %s GiB (cap %s GiB, %s limit) on a %s GiB box"
            % (n.get("measured_gb", "?"), n.get("cap_gb", "?"), kind,
               n.get("box_mem_gb", "?"))
        )
        if n.get("swap_gb"):
            lines.append("  of which swap: %s GiB" % n["swap_gb"])
    else:
        lines.append(
            "  cpu:     %s cores (cap %s, %s limit) on a %s-core box"
            % (n.get("measured_cores", "?"), n.get("cap_cores", "?"), kind,
               n.get("box_cores", "?"))
        )
    if n.get("ran_for_s"):
        lines.append("  ran for: %ds before the watchdog acted"
                     % int(n["ran_for_s"]))
    if kind == "pressure":
        lines.append(
            "  reason:  the box as a whole ran short of memory and this "
            "session was its largest contributor — no single cap was exceeded."
        )
    lines.append("")
    lines.append(
        "This workload is a candidate for remote compute. %s"
        % (n.get("remote_hint") or "Use the `ssh` domain to reach the "
           "configured remote boxes and HPC clusters.")
    )
    if action == "terminated":
        lines.append(
            "If it genuinely must run locally, take a bounded exemption first: "
            "`awm compute grant --mem-gb <N> --ttl-min <M> --reason \"...\"`, "
            "then re-run."
        )
    return "\n".join(lines)


def render_pressure(box: dict) -> str:
    return (
        "⚠️ Box under load: %s GiB RAM available of %s, %s/%s cores "
        "busy%s. Prefer lower parallelism (-j2, fewer workers) or run this on "
        "remote compute via the `ssh` domain. Heavy local jobs may be "
        "deprioritized (CPU) or stopped (memory)."
        % (box.get("mem_available_gb", "?"), box.get("box_mem_gb", "?"),
           box.get("cpu_busy_cores", "?"), box.get("box_cores", "?"),
           ", swap in use" if box.get("swap_pressure") else "")
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - fail open on any malformed input
        return 0

    event = payload.get("hook_event_name") or ""
    session_id = payload.get("session_id") or os.environ.get(
        "CLAUDE_CODE_SESSION_ID", "")
    if not session_id:
        return 0

    parts: list[str] = []
    try:
        for notice in drain(session_id):
            parts.append(render(notice))
    except Exception:  # noqa: BLE001
        pass

    if event == "PreToolUse":
        try:
            box = read_box()
            if box and box.get("loaded"):
                parts.append(render_pressure(box))
        except Exception:  # noqa: BLE001
            pass

    if not parts:
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event or "PostToolUse",
            "additionalContext": "\n\n".join(parts),
        }
    }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - never break a tool call
        sys.exit(0)

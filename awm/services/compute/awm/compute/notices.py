"""Telling the agent what happened — and where the work should have gone.

The kill is enforcement; the message is the behaviour change. An agent that
sees a bare ``Killed`` learns nothing except that the box is flaky. An agent
that reads "your embedding run was using 41 GiB of a 68 GiB box, here are the
clusters it belongs on" changes what it does next time.

Delivery is a drop-file per undelivered action, in a per-session directory. The
watchdog writes; the Claude Code hooks drain. That decoupling is what makes it
work for a *detached* job nobody was blocked on — there is no tool result to
attach to, so the notice simply waits for the agent's next tool call.

The join is exact rather than heuristic: the session id in the notice is read
from the killed process's own environment, and the session id in the hook
payload is the same string.

Paths here are resolved from ``$HOME`` (or an explicit override), never from
``AWM_WORKSPACE`` — that variable points at a worktree for sandbox-spawned
agents, which would scatter notices where no hook is looking.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

GIB = 1024 ** 3


def notice_root() -> Path:
    override = os.environ.get("AWM_COMPUTE_NOTICE_DIR")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".claude" / "compute-notices"


def session_dir(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:128]
    return notice_root() / safe


BOX_SNAPSHOT = "_box.json"


# -- writing (watchdog side) ------------------------------------------------


def write_notice(session_id: str, payload: dict) -> Path:
    d = session_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{time.time():.6f}-{payload.get('target_pid', 0)}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.rename(path)  # atomic: a hook never reads a half-written notice
    return path


def write_box_snapshot(payload: dict) -> None:
    root = notice_root()
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / (BOX_SNAPSHOT + ".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.rename(root / BOX_SNAPSHOT)


# -- reading (hook side; mirrored in hooks/compute_hook.py) ------------------


def drain(session_id: str) -> list[dict]:
    d = session_dir(session_id)
    out: list[dict] = []
    try:
        files = sorted(p for p in d.iterdir() if p.suffix == ".json")
    except OSError:
        return out
    for path in files:
        try:
            out.append(json.loads(path.read_text()))
        except (OSError, json.JSONDecodeError):
            pass
        finally:
            try:
                path.unlink()
            except OSError:
                pass
    return out


def read_box_snapshot(max_age_s: float = 30.0) -> dict | None:
    path = notice_root() / BOX_SNAPSHOT
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - float(data.get("wall_ts", 0)) > max_age_s:
        return None  # stale: the watchdog is down, say nothing
    return data


# -- rendering --------------------------------------------------------------

DEFAULT_REMOTE_HINT = (
    "Remote compute is available through the `ssh` domain "
    "(`ssh(verb=\"connect\", args={host: ...})`, then run the job over that "
    "connection). Configured targets include the Sockeye and Fir HPC clusters "
    "and the lab boxes (cosmos, crux, mira, chamois, vega)."
)


def render(notice: dict) -> str:
    """The text an agent reads. Written to be actionable, not scolding."""
    action = notice.get("action", "acted on")
    metric = notice.get("metric", "resource")
    kind = notice.get("kind", "")
    cmd = (notice.get("cmdline") or "")[:300]
    ran_for = notice.get("ran_for_s")
    lines: list[str] = []

    if action == "terminated":
        lines.append(
            "⛔ A local job started by this session was STOPPED by the awm "
            "compute watchdog."
        )
    elif action == "deprioritized":
        lines.append(
            "🐌 A local job started by this session was DEPRIORITIZED (renice "
            f"{notice.get('nice', 19)}) by the awm compute watchdog. It has "
            "not been killed and will still finish — it now yields to "
            "interactive work."
        )
    else:
        lines.append(f"awm compute watchdog: {action}.")

    lines.append(f"  command: {cmd}")
    if metric == "memory":
        lines.append(
            f"  memory:  {notice.get('measured_gb', '?')} GiB "
            f"(cap {notice.get('cap_gb', '?')} GiB, {kind} limit) on a "
            f"{notice.get('box_mem_gb', '?')} GiB box"
        )
        if notice.get("swap_gb"):
            lines.append(f"  of which swap: {notice['swap_gb']} GiB")
    else:
        lines.append(
            f"  cpu:     {notice.get('measured_cores', '?')} cores "
            f"(cap {notice.get('cap_cores', '?')}, {kind} limit) on a "
            f"{notice.get('box_cores', '?')}-core box"
        )
    if ran_for:
        lines.append(f"  ran for: {int(ran_for)}s before the watchdog acted")
    if kind == "pressure":
        lines.append(
            "  reason:  the box as a whole ran short of memory and this "
            "session was the largest contributor — no single cap was exceeded."
        )

    lines.append("")
    lines.append(
        "This workload is a candidate for remote compute. "
        + (notice.get("remote_hint") or DEFAULT_REMOTE_HINT)
    )
    if action == "terminated":
        lines.append(
            "If it genuinely must run locally, take a bounded exemption first: "
            "`awm compute grant --mem-gb <N> --ttl-min <M> --reason \"...\"` "
            "and then re-run."
        )
    return "\n".join(lines)


def render_pressure(box: dict) -> str:
    """The pre-launch heads-up. One line, no scolding, actionable."""
    return (
        "⚠️ Box under load: "
        f"{box.get('mem_available_gb', '?')} GiB RAM available of "
        f"{box.get('box_mem_gb', '?')}, "
        f"{box.get('cpu_busy_cores', '?')}/{box.get('box_cores', '?')} cores busy"
        f"{', swap in use' if box.get('swap_pressure') else ''}. "
        "Prefer lower parallelism (-j2, fewer workers) or run this on remote "
        "compute via the `ssh` domain. Heavy local jobs may be deprioritized "
        "(CPU) or stopped (memory)."
    )

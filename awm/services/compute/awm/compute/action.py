"""Picking a victim, proving it is safe to touch, and touching it.

Two remedies, chosen by what actually breaks this box:

**CPU → deprioritise, never kill.** Sixteen cores saturated is sluggish, not
frozen, and it fixes itself when the job ends. Renicing the job tree costs
nothing, loses no work, restores interactive responsiveness within a scheduling
quantum, and is reversible. Killing a four-hour build because it used nine
cores would be pure loss.

**Memory → terminate.** There is no throttling equivalent, and swap thrash is
the thing that actually wedges the machine.

Everything else here is the refusal to act. :func:`assert_safe` re-samples and
checks eight things before any signal, and a failed check records a refusal
rather than proceeding. The protected set is explicit because it has to be:
an MCP server carries ``CLAUDE_CODE_SESSION_ID`` exactly like a build does, and
under this service's own attribution rule the two are indistinguishable. So is
the gateway, so is every service, so is a dev sandbox, so are the ``claude``
binaries themselves. None of them is inferable — they are listed.

On restoring priority: ``RLIMIT_NICE`` on this box is ``(0, 0)``, which means
an unprivileged process can raise a nice value but never lower it again. So
restore goes through ``sudo -n renice`` when passwordless sudo is available,
and when it is not, the deprioritisation stands for the job's lifetime and the
record says so. That is a degraded outcome, not a broken one: the job still
finishes.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass

from awm.compute.probe import Proc, read_cmdline, read_stat
from awm.compute.usage import subtree

log = logging.getLogger("awm.compute.action")

NICE_FLOOR = 19

#: Command basenames that are never a victim whatever else they are doing.
#:
#: ``ssh`` and friends are here because an SSH client is never the local
#: resource hog — the work is on the other end — while killing one can cost a
#: live ControlMaster to a cluster, and on at least one of ours a failed
#: reconnect burns an MFA attempt toward an account lockout. The asymmetry is
#: absolute, so the rule is too.
PROTECTED_BASENAMES: frozenset[str] = frozenset({
    "ssh", "sshd", "ssh-agent", "autossh", "scp", "sftp", "mosh", "mosh-server",
    "claude", "tmux", "systemd", "init", "dbus-daemon", "gpg-agent",
})

#: Anything whose command line matches is never a victim and never a member of
#: a victim's group. These are NOT inferable from the attribution rule — an MCP
#: server carries ``CLAUDE_CODE_SESSION_ID`` exactly like a build does — so
#: they are enumerated. Verified against a live snapshot of this box, which is
#: also where the surprises came from: the production gateway was started from
#: an agent's shell and therefore carries that agent's session id, and every
#: detached SSH ControlMaster carries the session id of whoever opened it.
PROTECTED: tuple[tuple[str, str], ...] = (
    ("claude-harness", r"(^|/)claude(\s|$)|claude\s+daemon|bg-pty-host|bg-spare"
                       r"|/versions/\d+\.\d+\.\d+|--session-id\b"),
    ("mcp-server",     r"(^|[\s/_-])mcp([\s/._-]|$)|mcp[-_]?server|awm-mcp"),
    ("awm-gateway",    r"-m\s+awm\.gateway\b|(^|/)awm\s+gateway\s+serve\b"
                       r"|(^|/)uvicorn\b|-m\s+uvicorn\b"),
    ("awm-service",    r"-m\s+awm\.\w+\.hub_adapter\b|(^|/)hub_adapter\.py\b"),
    ("awm-sandbox",    r"awm/gateway/dev/run\.sh|(^|/)awm\s+dev\s+(start|shadow)\b"),
    # The dsh service spawns the harness in its own session, so the node process
    # is not covered by the awm-service pattern that protects its supervisor —
    # and it is exactly the shape of a victim: a long-lived node process, idle
    # until it streams, carrying the session id of the agent whose shell started
    # the gateway. Only the harness itself; what it spawns to do work is work.
    # Matched on the launcher path, because nothing on the command line is
    # called `dsh` any more: the harness is built from the deepseek-harness
    # fork and started as `node …/apps/cli/lib/bin.js`. The old registry
    # runtime path is kept so a node that has not been repointed stays covered.
    ("dsh-harness",    r"services/dsh/runtime/node_modules"
                       r"|deepseek-harness/\S*/apps/cli/lib/bin\.js"
                       r"|(^|/)dsh\s+.*--profile\s+web\b"),
    ("ssh-tunnel",     r"(^|/)ssh\s+.*(-[A-Za-z]*[NMWfL]|ControlMaster|ProxyCommand)"),
    ("init",           r"^/sbin/init\b|^/lib/systemd/systemd\b|^systemd\b"),
)
_PROTECTED_RE = tuple((name, re.compile(pat)) for name, pat in PROTECTED)


_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "ksh"})


def _argv0_basename(cmdline: str) -> str:
    head = cmdline.split(" ", 1)[0]
    return head.rsplit("/", 1)[-1]


def _is_command_shell(cmdline: str) -> bool:
    """``bash -c …`` — the shape every Bash tool call takes."""
    parts = cmdline.split(None, 2)
    return (
        len(parts) >= 2
        and _argv0_basename(cmdline) in _SHELLS
        and parts[1] == "-c"
    )


def protected_reason(cmdline: str) -> str | None:
    """Why this process may never be touched, or ``None`` if it may.

    Called for the victim *and* for every member of its subtree, so one
    protected process anywhere below a candidate saves the whole tree.

    A ``bash -c`` wrapper is exempt from the command-line patterns, and that
    exemption is load-bearing in both directions. A Bash tool call's whole
    script is its command line, so without it any agent that so much as greps
    for ``awm.gateway`` would make itself unkillable — a hole wide enough to
    walk through deliberately. Nothing is lost by it: if such a shell really
    did launch infrastructure, the process is in its subtree, and the subtree
    scan refuses the kill on the child's behalf.
    """
    if not cmdline:
        # A process whose command line we cannot read is one we do not
        # understand. Refuse it.
        return "unreadable"
    if _argv0_basename(cmdline) in PROTECTED_BASENAMES:
        return "protected-binary"
    if _is_command_shell(cmdline):
        return None
    for name, rx in _PROTECTED_RE:
        if rx.search(cmdline):
            return name
    return None


@dataclass(slots=True)
class Candidate:
    """A job root proposed as the victim, with everything the record needs."""

    pid: int
    start_ticks: int
    pgid: int
    cmdline: str
    n_procs: int
    rss_estimate_b: int
    cpu_cores: float
    age_s: float


def select_victim(
    metric: str,
    session_pids: set[int],
    job_roots: list[int],
    procs: dict[int, Proc],
    delta_ticks: dict[tuple[int, int], int],
    dt: float,
    clk_tck: int,
    uptime_ticks: int,
) -> tuple[Candidate | None, list[Candidate]]:
    """Rank a session's job roots by the metric they are actually driving.

    Job roots — the shallowest processes in the session whose parent is outside
    it — are exactly the shell a Bash tool call launched, or the root of a
    detached job. Targeting an arbitrary descendant instead would kill a
    compiler and leave the build to spawn another.

    Ties break toward the *youngest* root: a job that has been running for
    hours has more to lose than one that started thirty seconds ago.
    """
    cands: list[Candidate] = []
    for root in job_roots:
        p = procs.get(root)
        if p is None:
            continue
        members = [m for m in subtree(root, procs) if m.pid in session_pids]
        if not members:
            continue
        ticks = sum(delta_ticks.get(m.key, 0) for m in members)
        cands.append(Candidate(
            pid=root,
            start_ticks=p.start_ticks,
            pgid=p.pgid,
            cmdline=read_cmdline(root) or p.comm,
            n_procs=len(members),
            rss_estimate_b=sum(m.rss_bytes for m in members),
            cpu_cores=(ticks / clk_tck / dt) if dt > 0 else 0.0,
            age_s=max(0.0, (uptime_ticks - p.start_ticks) / clk_tck),
        ))
    if not cands:
        return None, []

    key = (lambda c: (c.rss_estimate_b, -c.age_s)) if metric == "memory" \
        else (lambda c: (c.cpu_cores, -c.age_s))
    cands.sort(key=key, reverse=True)

    for c in cands:
        if protected_reason(c.cmdline) is None:
            return c, cands
    return None, cands


class Unsafe(Exception):
    """A pre-action assertion failed. Records a refusal; never a signal."""


def assert_safe(
    cand: Candidate,
    session_id: str,
    sid_by_pid: dict[int, str | None],
    procs: dict[int, Proc],
    *,
    self_pids: set[int],
) -> list[Proc]:
    """Re-check everything immediately before signalling. Returns the group.

    Raises :class:`Unsafe` on any failure — which the caller records as a
    refusal. Refusing wrongly costs a slow box; signalling wrongly costs an
    outage, so every check here fails toward refusal.
    """
    live = read_stat(cand.pid)
    if live is None or live.start_ticks != cand.start_ticks:
        raise Unsafe("target exited or its pid was recycled")
    if sid_by_pid.get(cand.pid) != session_id:
        raise Unsafe("target no longer attributed to the offending session")
    if cand.pid in self_pids:
        raise Unsafe("target is the watchdog itself or one of its ancestors")

    members = subtree(cand.pid, procs)
    member_pids = {m.pid for m in members}

    # Killing a process group equals killing a subtree only when the job leads
    # its own group. Usually true for a shell-launched job; not guaranteed, so
    # it is checked rather than assumed.
    group = [p for p in procs.values() if p.pgid == live.pgid]
    stray = [p.pid for p in group if p.pid not in member_pids]
    if stray:
        raise Unsafe(f"process group {live.pgid} reaches outside the subtree: {stray[:5]}")

    for m in members:
        cmd = read_cmdline(m.pid)
        why = protected_reason(cmd)
        if why is not None:
            raise Unsafe(f"subtree contains a protected process ({why}): pid {m.pid}")
        if m.pid in self_pids:
            raise Unsafe(f"subtree contains the watchdog itself: pid {m.pid}")
    return members


# -- remedies ---------------------------------------------------------------


def deprioritize(members: list[Proc]) -> dict:
    """Raise nice on the whole job tree. Never kills, never loses work."""
    moved, failed = [], []
    for m in members:
        try:
            if os.getpriority(os.PRIO_PROCESS, m.pid) >= NICE_FLOOR:
                continue
            os.setpriority(os.PRIO_PROCESS, m.pid, NICE_FLOOR)
            moved.append(m.pid)
        except OSError as exc:
            failed.append({"pid": m.pid, "error": str(exc)})
    return {"reniced": moved, "failed": failed, "nice": NICE_FLOOR}


def can_restore_priority() -> bool:
    """Whether we can undo a renice — needs privilege this box denies us.

    ``RLIMIT_NICE`` is ``(0, 0)`` here, so the direct call always fails and
    passwordless sudo is the only route back.
    """
    try:
        return subprocess.run(
            ["sudo", "-n", "true"], capture_output=True, timeout=3.0
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def restore_priority(pids: list[int]) -> dict:
    """Put a deprioritised tree back to nice 0 once pressure has cleared."""
    restored, failed = [], []
    todo = []
    for pid in pids:
        try:
            os.setpriority(os.PRIO_PROCESS, pid, 0)
            restored.append(pid)
        except OSError:
            todo.append(pid)
    if todo:
        try:
            out = subprocess.run(
                ["sudo", "-n", "renice", "-n", "0", "-p", *map(str, todo)],
                capture_output=True, text=True, timeout=5.0,
            )
            if out.returncode == 0:
                restored.extend(todo)
            else:
                failed.append({"pids": todo, "error": out.stderr.strip()[:200]})
        except (OSError, subprocess.SubprocessError) as exc:
            failed.append({"pids": todo, "error": str(exc)})
    return {"restored": restored, "failed": failed}


def terminate(pid: int, *, grace_s: float = 5.0) -> dict:
    """SIGTERM the job's process group, wait, then SIGKILL.

    Deliberately modelled on ``awm.gateway.hub.supervisor.kill_pid_group``,
    minus its ``waitpid`` reaping step — these are not our children, so there
    is nothing for us to reap and a blind ``waitpid`` would be wrong.
    """
    result: dict = {"pid": pid, "signals": []}
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError) as exc:
        result["outcome"] = f"already gone ({exc.__class__.__name__})"
        return result
    result["pgid"] = pgid
    try:
        os.killpg(pgid, signal.SIGTERM)
        result["signals"].append("SIGTERM")
    except (ProcessLookupError, PermissionError) as exc:
        result["outcome"] = f"SIGTERM failed: {exc}"
        return result

    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            result["outcome"] = "exited on SIGTERM"
            return result
        time.sleep(0.1)

    try:
        os.killpg(pgid, signal.SIGKILL)
        result["signals"].append("SIGKILL")
    except (ProcessLookupError, PermissionError):
        result["outcome"] = "exited during grace"
        return result
    result["outcome"] = "killed"
    return result


def ancestor_pids(pid: int | None = None) -> set[int]:
    """Our own pid and every ancestor — the set we must never signal."""
    pid = os.getpid() if pid is None else pid
    out: set[int] = set()
    for _ in range(64):
        if pid <= 1 or pid in out:
            break
        out.add(pid)
        p = read_stat(pid)
        if p is None:
            break
        pid = p.ppid
    return out

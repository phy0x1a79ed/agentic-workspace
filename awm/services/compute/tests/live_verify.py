#!/usr/bin/env python3
"""Synthetic-hog verification — real processes, real signals, tiny caps.

Run explicitly; not part of the default suite, because it spawns processes,
kills them, and takes about a minute:

    PYTHONPATH=<dist roots> python tests/live_verify.py

Everything here runs against **deliberately lowered** caps — a 1 GiB memory
ceiling and a 2-core CPU ceiling — so proving the watchdog works never requires
driving the shared box toward a real freeze. That is also why the watcher is
scoped with ``only_session`` to the sessions this script invents: at a 1 GiB
ceiling, every real agent on the box would otherwise be in violation.

State is redirected to a throwaway workspace so this cannot touch the
production service DB, and to a throwaway notice directory so it cannot drop
notices into a live agent's hook path.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time

WORKDIR = tempfile.mkdtemp(prefix="compute-verify-")
# Drop our OWN session id before anything else. This script is normally started
# from an agent's shell, so without this every hog it spawns falls back to
# inheriting the real session that launched the verification — including the
# "unattributed" negative control, which would then be anything but.
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
os.environ["AWM_WORKSPACE"] = WORKDIR
os.environ["AWM_COMPUTE_NOTICE_DIR"] = os.path.join(WORKDIR, "notices")
os.environ.setdefault("AWM_HUB_URL", "http://127.0.0.1:7819")  # arm-eligible

from awm.compute import notices, store  # noqa: E402
from awm.compute.attribution import SESSION_VAR  # noqa: E402
from awm.compute.watcher import Watcher  # noqa: E402

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
results: list[tuple[bool, str]] = []


def check(ok: bool, what: str, detail: str = "") -> None:
    results.append((ok, what))
    print(f"  {PASS if ok else FAIL}  {what}{('  — ' + detail) if detail else ''}")


def spawn(script: str, session: str | None, tag: str) -> subprocess.Popen:
    """Start a hog in its own process group, exactly as a Bash tool call does."""
    env = dict(os.environ)
    env.pop(SESSION_VAR, None)
    if session:
        env[SESSION_VAR] = session
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(script)],
        env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


MEM_HOG = """
    import time
    blocks = []
    while True:
        blocks.append(bytearray(128 * 1024 * 1024))
        for b in blocks[-1:]:
            b[::4096] = b"x" * (len(b) // 4096)
        time.sleep(0.25)
"""

CPU_HOG = """
    import os, time
    for _ in range(3):
        if os.fork() == 0:
            while True: pass
    while True: pass
"""

BUILD = """
    import subprocess, sys
    # Hundreds of sub-second children, every one reaped before the next sample:
    # invisible to any sampler that ignores cutime/cstime.
    for _ in range(400):
        subprocess.run([sys.executable, "-c", "sum(range(400000))"])
"""


def fresh_watcher(session: str, **settings) -> Watcher:
    store.init()
    store.set_settings({
        "armed": True, "dry_run": False, "only_session": session,
        "dwell_mem_s": 4.0, "dwell_cpu_s": 6.0, "quiet_period_s": 1.0,
        **settings,
    })
    w = Watcher()
    w.setup()
    return w


def drive(w: Watcher, seconds: float, step: float = 1.0, until=None) -> float:
    """Tick the watcher for up to ``seconds``; return how long it actually ran."""
    t0 = time.monotonic()
    deadline = t0 + seconds
    while time.monotonic() < deadline:
        w.tick()
        if until is not None and until():
            break
        time.sleep(step)
    return time.monotonic() - t0


def alive(p: subprocess.Popen) -> bool:
    return p.poll() is None


def kill_group(p: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# ---------------------------------------------------------------------------


def test_memory_hog_is_stopped():
    print("\n[1] memory hog vs a 1 GiB hard ceiling")
    sid = "verify-mem"
    total_gb = 73769005056 / 1024 ** 3
    w = fresh_watcher(sid, mem_reserve_gb=total_gb - 1.0)
    hog = spawn(MEM_HOG, sid, "mem")
    try:
        elapsed = drive(w, 25.0, until=lambda: not alive(hog))
        check(not alive(hog), "allocator was stopped",
              f"after {elapsed:.0f}s (4s dwell + growth time)")
        drained = notices.drain(sid)
        check(bool(drained), "a notice was written for the owning session")
        if drained:
            n = drained[0]
            check(n["action"] == "terminated", "notice says terminated")
            check(n["metric"] == "memory", "notice names the memory limit")
            check("remote" in notices.render(n).lower(),
                  "notice points at remote compute")
        rows = store.recent_decisions(20)
        acted = [r for r in rows if r["outcome"] == "terminated"]
        check(bool(acted), "the kill is in the ledger")
        if acted:
            d = acted[0]["detail"]
            check("pss_gb" in d, "the ledger records the ACCURATE measurement",
                  f"pss={d.get('pss_gb')} GiB swap={d.get('swap_gb')} GiB")
    finally:
        kill_group(hog)


def test_cpu_hog_is_deprioritized_never_killed():
    print("\n[2] 4-core spinner vs a 2-core hard ceiling")
    sid = "verify-cpu"
    w = fresh_watcher(sid, cpu_headroom_cores=14)   # nproc-14 = 2 cores
    hog = spawn(CPU_HOG, sid, "cpu")
    try:
        drive(w, 25.0)
        check(alive(hog), "the spinner is STILL RUNNING — CPU never kills")
        check(sid in w.deprioritized, "the session was deprioritized")
        nices = []
        for pid in w.deprioritized.get(sid, {}).get("pids", []):
            try:
                nices.append(os.getpriority(os.PRIO_PROCESS, pid))
            except OSError:
                pass
        check(bool(nices) and all(n >= 19 for n in nices),
              "every process in the job tree is at nice 19", str(nices))
        drained = notices.drain(sid)
        check(bool(drained) and drained[0]["action"] == "deprioritized",
              "the agent is told it was slowed, not killed")
        rows = store.recent_decisions(20)
        check(not any(r["action"] == "terminate" and r["outcome"] == "terminated"
                      for r in rows),
              "no kill was recorded for a CPU violation")
    finally:
        kill_group(hog)


def test_deprioritization_is_lifted_on_recovery():
    print("\n[3] deprioritization lifts when the session drops back under")
    sid = "verify-cpu-restore"
    w = fresh_watcher(sid, cpu_headroom_cores=14)
    hog = spawn(CPU_HOG, sid, "cpu2")
    try:
        drive(w, 20.0)
        if sid not in w.deprioritized:
            check(False, "precondition: session was deprioritized")
            return
        pids = list(w.deprioritized[sid]["pids"])
        kill_group(hog)
        time.sleep(1.0)
        drive(w, 6.0)
        check(sid not in w.deprioritized, "the renice record was cleared")
        if not w.can_restore:
            check(True, "restore skipped: no privilege to lower nice on this box "
                        "(RLIMIT_NICE is (0,0)) — recorded, not retried")
        else:
            survivors = [p for p in pids if os.path.exists(f"/proc/{p}")]
            check(all(os.getpriority(os.PRIO_PROCESS, p) == 0 for p in survivors),
                  "surviving processes are back at nice 0", str(survivors))
    finally:
        kill_group(hog)


def test_parallel_build_is_visible():
    print("\n[4] a build of 400 sub-second children is visible to a 2 s sampler")
    sid = "verify-build"
    w = fresh_watcher(sid, cpu_headroom_cores=1, dwell_cpu_s=999.0)  # observe only
    hog = spawn(BUILD, sid, "build")
    try:
        peak = 0.0
        for _ in range(12):
            w.tick()
            u = w.usages.get(sid)
            if u:
                peak = max(peak, u.cpu_cores)
            time.sleep(1.0)
        check(peak > 0.3,
              "reaped-children CPU was measured", f"peak {peak:.2f} cores")
    finally:
        kill_group(hog)


def test_a_grant_suppresses_action():
    print("\n[5] a bounded grant suppresses the action")
    sid = "verify-grant"
    total_gb = 73769005056 / 1024 ** 3
    w = fresh_watcher(sid, mem_reserve_gb=total_gb - 1.0)
    store.add_grant(sid, reason="verification", mem_gb=8.0, cpu_cores=None,
                    ttl_s=600.0)
    w.reload_settings()
    hog = spawn(MEM_HOG, sid, "grant")
    try:
        drive(w, 18.0)
        check(alive(hog), "the granted job was left alone")
        check(not notices.drain(sid), "no notice was sent")
    finally:
        kill_group(hog)
        store.revoke_grants(sid)


def test_the_users_own_shell_is_out_of_scope():
    """The negative control that matters: the user's own terminal work.

    Getting this right in the harness took a correction. Clearing
    ``CLAUDE_CODE_SESSION_ID`` from *this script's* ``os.environ`` does not
    change ``/proc/<pid>/environ``, which is fixed at exec — so a hog spawned
    here still descends from an agent-owned process and is, correctly,
    attributed to it. A genuine user-shell process descends from init, so the
    control double-forks to get there.
    """
    print("\n[6] negative control: a process outside every agent tree")
    from awm.compute.attribution import Attributor
    from awm.compute.probe import read_cmdline, scan

    sid = "verify-negative"
    marker = "compute-verify-user-shell"
    w = fresh_watcher(sid, cpu_headroom_cores=14)
    subprocess.run(
        ["setsid", "--fork", sys.executable, "-c",
         f"# {marker}\n" + textwrap.dedent(CPU_HOG)],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.0)
    procs = scan()
    hogs = [pid for pid in procs if marker in read_cmdline(pid)]
    try:
        if not hogs:
            check(False, "precondition: the orphaned spinner started")
            return
        sids = Attributor().resolve_all(procs)
        check(all(sids.get(pid) is None for pid in hogs),
              "it carries no session id and descends from no agent")
        drive(w, 12.0)
        check(all(os.path.exists(f"/proc/{pid}") for pid in hogs),
              "the spinner is untouched")
        check(not any(r["target_pid"] in hogs for r in store.recent_decisions(50)),
              "it appears in no decision")
    finally:
        for pid in hogs:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def test_the_arming_guard():
    print("\n[7] arming guard: a sandbox origin can never arm")
    saved = os.environ["AWM_HUB_URL"]
    try:
        os.environ["AWM_HUB_URL"] = "http://127.0.0.1:7861"   # feat-dag sandbox
        store.set_settings({"armed": True, "dry_run": False})
        w = Watcher()
        w.setup()
        check(not w.arm_eligible, "sandbox origin is not arm-eligible")
        check(not w.armed, "armed=true in settings is overridden to false")
    finally:
        os.environ["AWM_HUB_URL"] = saved


def test_duty_cycle():
    print("\n[8] duty cycle under the user's 1%-of-a-core budget")
    w = fresh_watcher("verify-duty")
    costs = []
    for _ in range(12):
        w.tick()
        if w.last_pass_cost_ms:
            costs.append(w.last_pass_cost_ms)
        time.sleep(0.5)
    if not costs:
        check(False, "no full pass ran during the duty measurement")
        return
    med = sorted(costs)[len(costs) // 2]
    duty = med / 2000.0 * 100.0
    check(duty < 1.0, "full pass every 2 s stays under 1% of one core",
          f"{med:.2f} ms/pass = {duty:.3f}%")
    check(med < 20.0, "a full pass costs single-digit milliseconds",
          f"{med:.2f} ms")


def main() -> int:
    print(f"compute watchdog live verification (state in {WORKDIR})")
    for fn in (
        test_memory_hog_is_stopped,
        test_cpu_hog_is_deprioritized_never_killed,
        test_deprioritization_is_lifted_on_recovery,
        test_parallel_build_is_visible,
        test_a_grant_suppresses_action,
        test_the_users_own_shell_is_out_of_scope,
        test_the_arming_guard,
        test_duty_cycle,
    ):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(False, f"{fn.__name__} raised", repr(exc))

    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for w in failed:
        print(f"  {FAIL}  {w}")
    shutil.rmtree(WORKDIR, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

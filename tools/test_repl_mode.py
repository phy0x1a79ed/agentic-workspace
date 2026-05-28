#!/usr/bin/env python3
"""End-to-end test for the operator's REPL mode.

`tools/probe_op.py <name>` with no subcommand drops into a line-oriented
REPL (`probe_op.py:575-593`) that reads commands from stdin via `input()`
and shells each one to the friend via `Session.exec_one`. This test pipes a
small command script in and asserts the friend executed it and the REPL
exited cleanly.

Requires:
    - tools/.env.probe (EMQX creds)
    - binary/target/release/probe built
    - tmux on PATH (used to give the friend a pty)
"""
from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

SCOPE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCOPE_ROOT / "tools"))

PROBE_BIN = SCOPE_ROOT / "binary" / "target" / "release" / "probe"
OPERATOR = SCOPE_ROOT / "tools" / "probe_op.py"
ENV_FILE = SCOPE_ROOT / "tools" / ".env.probe"

FRIEND_READY_TIMEOUT = 25.0
REPL_TIMEOUT = 45.0


def load_env() -> dict:
    env = {}
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def tmux(*args, check=True):
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check)


def pane_text(session: str) -> str:
    res = tmux("capture-pane", "-p", "-t", session, check=False)
    return res.stdout if res.returncode == 0 else ""


def wait_for(predicate, timeout: float, interval: float = 0.25) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def spawn_friend(session: str, name: str, env: dict) -> None:
    cmd = f"{PROBE_BIN} --name {name} --no-consent --mqtt-pass '{env['EMQX_PASS']}'"
    full_env = {**os.environ, "RUST_LOG": "probe=info", "EMQX_PASS": env["EMQX_PASS"]}
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-x", "200", "-y", "60", cmd],
        check=True,
        env=full_env,
    )


def main() -> int:
    if not PROBE_BIN.is_file():
        print(f"FATAL: {PROBE_BIN} not found", file=sys.stderr)
        return 2
    if shutil.which("tmux") is None:
        print("FATAL: tmux not on PATH", file=sys.stderr)
        return 2
    env = load_env()
    for k in ("EMQX_URL", "EMQX_USER", "EMQX_PASS"):
        if not env.get(k):
            print(f"FATAL: {k} missing in {ENV_FILE}", file=sys.stderr)
            return 2

    name = f"repl-{uuid.uuid4().hex[:8]}"
    session = f"probe-repl-{uuid.uuid4().hex[:8]}"
    repl_proc: subprocess.Popen | None = None

    print(f"=== test_repl_mode: name={name} ===")

    passed = 0
    failed = 0
    try:
        spawn_friend(session, name, env)
        if not wait_for(lambda: "subscribed; waiting for operator" in pane_text(session), FRIEND_READY_TIMEOUT):
            print(f"FAIL: friend never reached subscribed state in {FRIEND_READY_TIMEOUT}s")
            print(f"--- pane ---\n{pane_text(session)}")
            return 1
        print("  friend ready")

        full_env = {**os.environ, **env, "PYTHONUNBUFFERED": "1"}
        repl_proc = subprocess.Popen(
            [sys.executable, "-u", str(OPERATOR), name],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
        )

        # The REPL prints "[connected to <name>] Ctrl-D to exit" then a "> "
        # prompt to stderr. We just pipe commands as input() lines. `exit`
        # is recognized by the REPL loop (probe_op.py:590).
        script = b"uname -n\nexit\n"
        try:
            stdout, stderr = repl_proc.communicate(input=script, timeout=REPL_TIMEOUT)
        except subprocess.TimeoutExpired:
            repl_proc.kill()
            stdout, stderr = repl_proc.communicate(timeout=5)
            failed += 1
            print(f"FAIL: REPL did not exit within {REPL_TIMEOUT}s")
            print(f"--- stdout ---\n{stdout.decode(errors='replace')[-1500:]}")
            print(f"--- stderr ---\n{stderr.decode(errors='replace')[-1500:]}")
            return 1

        rc = repl_proc.returncode
        # The friend ran inside the same tmux pane, which on this box is the
        # local host. We don't pin the hostname value (CI hosts vary); we
        # just require *some* alphanumeric line in stdout that isn't empty.
        out_text = stdout.decode(errors="replace").strip()
        if rc == 0 and out_text:
            passed += 1
            print(f"  PASS REPL (rc={rc}, hostname={out_text.splitlines()[-1]!r})")
        else:
            failed += 1
            print(f"FAIL REPL: rc={rc}, stdout={stdout!r}, stderr={stderr.decode(errors='replace')[-500:]!r}")
    finally:
        try:
            if repl_proc and repl_proc.poll() is None:
                repl_proc.send_signal(signal.SIGTERM)
                try:
                    repl_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    repl_proc.kill()
        except Exception:
            pass
        try:
            tmux("kill-session", "-t", session, check=False)
        except Exception:
            pass

    print(f"\n=== results: {passed} passed, {failed} failed ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

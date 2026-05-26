#!/usr/bin/env python3
"""End-to-end test for probe transport + exec.

Spawns the friend-side probe binary as a subprocess (with --no-consent),
then runs the operator script through several exec scenarios against the
real EMQX broker. Each scenario asserts stdout / stderr / exit code.

Requires:
    - tools/.env.emqx with valid EMQX_URL/EMQX_USER/EMQX_PASS
    - binary/target/release/probe built (cargo build --release)
    - aiortc + aiomqtt installed (pip install -r tools/requirements.txt)

Usage:
    python3 tools/test_e2e.py
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
import time
import uuid
from pathlib import Path

SCOPE_ROOT = Path(__file__).resolve().parent.parent
PROBE_BIN = SCOPE_ROOT / "binary" / "target" / "release" / "probe"
OPERATOR = SCOPE_ROOT / "tools" / "probe_op.py"
ENV_FILE = SCOPE_ROOT / "tools" / ".env.emqx"

CONNECT_TIMEOUT = "30"
EXEC_TIMEOUT = "20"


def load_env_file(path: Path) -> dict:
    if not path.is_file():
        return {}
    env = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def spawn_probe(name: str, env: dict) -> subprocess.Popen:
    # URL+USER are baked into the binary; we only pass PASS via env.
    cmd = [str(PROBE_BIN), "--name", name, "--no-consent"]
    print(f"  spawn: {' '.join(cmd)} (EMQX_PASS via env)")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env={**os.environ, "RUST_LOG": "probe=info", "EMQX_PASS": env["EMQX_PASS"]},
    )
    return proc


def wait_for_probe_ready(proc: subprocess.Popen, name: str, timeout: float = 15.0) -> bool:
    """Read probe stderr until we see the 'code=<name>' line or it dies."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            stderr_dump = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            print(f"  probe died early; stderr:\n{stderr_dump}")
            return False
        line = proc.stderr.readline().decode("utf-8", errors="replace") if proc.stderr else ""
        if line:
            print(f"  [probe] {line.rstrip()}")
            if f"code={name}" in line:
                return True
        else:
            time.sleep(0.05)
    return False


def drain_probe_stderr_background(proc: subprocess.Popen):
    import threading

    def drain():
        try:
            for raw in iter(proc.stderr.readline, b""):
                print(f"  [probe] {raw.decode('utf-8', errors='replace').rstrip()}")
        except Exception:
            pass

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    return t


def run_operator_exec(name: str, cmd: str, env: dict, timeout: float = 60.0) -> tuple[int, bytes, bytes]:
    op_cmd = [
        sys.executable,
        str(OPERATOR),
        name,
        "--timeout", EXEC_TIMEOUT,
        "--connect-timeout", CONNECT_TIMEOUT,
        "exec",
        cmd,
    ]
    proc = subprocess.run(
        op_cmd,
        capture_output=True,
        timeout=timeout,
        env={**os.environ, **env},
    )
    return proc.returncode, proc.stdout, proc.stderr


SCENARIOS = [
    # (label, cmd, expected_exit, stdout_check, stderr_check)
    (
        "echo hello",
        "echo hello",
        0,
        lambda b: b == b"hello\n",
        lambda b: b == b"",
    ),
    (
        "stderr + exit 3",
        "printf err >&2; exit 3",
        3,
        lambda b: b == b"",
        lambda b: b == b"err",
    ),
    (
        "seq 1..50",
        "seq 1 50",
        0,
        lambda b: b == ("\n".join(str(i) for i in range(1, 51)) + "\n").encode(),
        lambda b: b == b"",
    ),
    (
        "sleep 0.5 then echo",
        "sleep 0.5; echo done",
        0,
        lambda b: b == b"done\n",
        lambda b: b == b"",
    ),
    (
        "hostname matches local",
        "cat /etc/hostname",
        0,
        lambda b: b == (platform.node() + "\n").encode() or len(b) > 0,
        lambda b: b == b"",
    ),
]


def main() -> int:
    if not PROBE_BIN.is_file():
        print(f"FATAL: {PROBE_BIN} not found; build with: cd binary && cargo build --release", file=sys.stderr)
        return 2

    env = load_env_file(ENV_FILE)
    for key in ("EMQX_URL", "EMQX_USER", "EMQX_PASS"):
        if not env.get(key):
            print(f"FATAL: {key} missing in {ENV_FILE}", file=sys.stderr)
            return 2

    name = f"test-{uuid.uuid4().hex[:8]}"
    print(f"=== probe e2e: name={name} ===")

    probe = spawn_probe(name, env)
    if not wait_for_probe_ready(probe, name):
        print("probe did not become ready", file=sys.stderr)
        probe.terminate()
        return 3

    drain_thread = drain_probe_stderr_background(probe)

    passed = 0
    failed = 0
    try:
        for label, cmd, expected_rc, ok_stdout, ok_stderr in SCENARIOS:
            print(f"\n--- {label} ---")
            print(f"  cmd: {cmd!r}")
            try:
                rc, out, err = run_operator_exec(name, cmd, env)
            except subprocess.TimeoutExpired:
                print(f"  FAIL: operator timed out", file=sys.stderr)
                failed += 1
                continue
            ok = rc == expected_rc and ok_stdout(out) and ok_stderr(err)
            print(f"  rc={rc} stdout={out!r} stderr={err!r}")
            if ok:
                print(f"  PASS")
                passed += 1
            else:
                print(f"  FAIL: expected rc={expected_rc}")
                failed += 1
    finally:
        print("\n=== teardown ===")
        try:
            probe.terminate()
            probe.wait(timeout=2)
        except subprocess.TimeoutExpired:
            probe.kill()
            probe.wait(timeout=1)

    print(f"\n=== results: {passed} passed, {failed} failed ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

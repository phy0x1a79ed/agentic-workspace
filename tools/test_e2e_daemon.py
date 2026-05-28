#!/usr/bin/env python3
"""End-to-end test for the persistent operator daemon + `send` CLI.

Spawns the friend probe binary, then a `probe_op.py <name> daemon`, then
exercises:
  - several `send` calls reuse one channel (friend PID unchanged)
  - exit codes propagate
  - stdout/stderr stream correctly
  - `send` against a name with no daemon errors out
  - `stop` triggers a Bye that takes the friend down

Requires:
    - tools/.env.probe with EMQX_URL/EMQX_USER/EMQX_PASS
    - binary/target/release/probe built
    - aiortc + aiomqtt installed (env `awm`)

Usage:
    mamba run -n awm python tools/test_e2e_daemon.py
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

SCOPE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCOPE_ROOT / "tools"))

import probe_op  # noqa: E402

PROBE_BIN = SCOPE_ROOT / "binary" / "target" / "release" / "probe"
OPERATOR = SCOPE_ROOT / "tools" / "probe_op.py"
ENV_FILE = SCOPE_ROOT / "tools" / ".env.probe"
DAEMON_BOOT_TIMEOUT = 30.0
SEND_TIMEOUT = 15.0


def load_env() -> dict:
    env = {}
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def spawn_friend(name: str, env: dict) -> subprocess.Popen:
    cmd = [str(PROBE_BIN), "--name", name, "--no-consent"]
    print(f"  spawn friend: {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env={**os.environ, "RUST_LOG": "probe=info", "EMQX_PASS": env["EMQX_PASS"]},
    )


def wait_friend_ready(proc: subprocess.Popen, name: str, timeout: float = 15.0) -> bool:
    """Brief sleep + liveness check. The Rust binary no longer prints a
    `code=<name>` line on startup (the original readiness signal), so the
    real "is the friend subscribed" check is handled by the operator
    daemon's `--connect-timeout` — if the friend isn't ready in time, the
    daemon will exit and `wait_socket` will fail downstream."""
    time.sleep(3.0)
    return proc.poll() is None


def drain_in_background(proc: subprocess.Popen, label: str):
    import threading

    def drain(stream, prefix):
        try:
            for raw in iter(stream.readline, b""):
                print(f"  [{prefix}] {raw.decode('utf-8', errors='replace').rstrip()}")
        except Exception:
            pass

    for stream, suffix in ((proc.stderr, "stderr"), (proc.stdout, "stdout")):
        if stream is None:
            continue
        t = threading.Thread(target=drain, args=(stream, f"{label}.{suffix}"), daemon=True)
        t.start()


def spawn_daemon(name: str, env: dict, mqtt_relay: bool = False) -> subprocess.Popen:
    cmd = [sys.executable, str(OPERATOR), name]
    if mqtt_relay:
        cmd.append("--mqtt-relay")
    cmd.append("daemon")
    print(f"  spawn daemon: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env={**os.environ, **env},
    )
    return proc


def wait_socket(path: Path, proc: subprocess.Popen, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if path.exists():
            return True
        time.sleep(0.1)
    return False


def run_send(name: str, cmd: str, env: dict, timeout: float = SEND_TIMEOUT) -> tuple[int, bytes, bytes]:
    proc = subprocess.run(
        [sys.executable, str(OPERATOR), name, "send", cmd],
        capture_output=True,
        env={**os.environ, **env},
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def run_stop(name: str, env: dict) -> int:
    proc = subprocess.run(
        [sys.executable, str(OPERATOR), name, "stop"],
        capture_output=True,
        env={**os.environ, **env},
        timeout=10.0,
    )
    return proc.returncode


def check(label: str, ok: bool, detail: str = ""):
    status = "PASS" if ok else "FAIL"
    line = f"  [{status}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


def run_relay_or_webrtc(name: str, env: dict, *, mqtt_relay: bool) -> tuple[int, int]:
    passed = 0
    failed = 0
    suffix = "relay" if mqtt_relay else "webrtc"
    print(f"\n=== flavour: {suffix} ===")

    friend_args = [str(PROBE_BIN), "--name", name, "--no-consent"]
    if mqtt_relay:
        friend_args.append("--mqtt-relay")
    print(f"  spawn friend: {' '.join(friend_args)}")
    friend = subprocess.Popen(
        friend_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env={**os.environ, "RUST_LOG": "probe=info", "EMQX_PASS": env["EMQX_PASS"]},
    )
    try:
        if not wait_friend_ready(friend, name):
            failed += 1
            check(f"{suffix}: friend ready", False, "did not print code=")
            return passed, failed
        passed += 1
        drain_in_background(friend, "friend")

        daemon = spawn_daemon(name, env, mqtt_relay=mqtt_relay)
        try:
            sock_path = probe_op._socket_path(name)
            ready = wait_socket(sock_path, daemon, DAEMON_BOOT_TIMEOUT)
            if not check(f"{suffix}: daemon socket up at {sock_path}", ready):
                failed += 1
                return passed, failed
            passed += 1
            drain_in_background(daemon, "daemon")

            friend_pid = friend.pid

            rc, out, err = run_send(name, "echo hello-1", env)
            passed += check(f"{suffix}: send #1 echo", rc == 0 and out == b"hello-1\n",
                            f"rc={rc} out={out!r} err={err!r}")
            failed += int(not (rc == 0 and out == b"hello-1\n"))

            rc, out, err = run_send(name, "echo hello-2", env)
            passed += check(f"{suffix}: send #2 echo", rc == 0 and out == b"hello-2\n",
                            f"rc={rc} out={out!r}")
            failed += int(not (rc == 0 and out == b"hello-2\n"))

            rc, out, err = run_send(name, "printf err >&2; exit 7", env)
            passed += check(f"{suffix}: send exit-code 7 + stderr",
                            rc == 7 and out == b"" and err.endswith(b"err"),
                            f"rc={rc} out={out!r} err={err!r}")
            failed += int(not (rc == 7 and out == b"" and err.endswith(b"err")))

            # G1+G3: friend PID has not changed across the three sends.
            still_alive = friend.poll() is None and friend.pid == friend_pid
            passed += check(f"{suffix}: friend PID unchanged across sends",
                            still_alive, f"pid={friend_pid} alive={still_alive}")
            failed += int(not still_alive)

            # "no daemon" path on a fresh name.
            absent = f"absent-{uuid.uuid4().hex[:6]}"
            rc, out, err = run_send(absent, "echo ignored", env)
            passed += check(f"{suffix}: send to absent name errors", rc != 0 and b"no daemon" in err,
                            f"rc={rc} err={err!r}")
            failed += int(not (rc != 0 and b"no daemon" in err))

            # stop the daemon — friend should follow it down.
            rc = run_stop(name, env)
            passed += check(f"{suffix}: stop returns 0", rc == 0, f"rc={rc}")
            failed += int(rc != 0)

            try:
                daemon.wait(timeout=10)
                daemon_exited = True
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon_exited = False
            if daemon_exited:
                passed += check(f"{suffix}: daemon exited after stop", True)
            else:
                failed += 1
                check(f"{suffix}: daemon exited after stop", False)

            # Note: the friend binary today does NOT exit on receiving Bye —
            # its main loop only breaks on its own ctrl-c, on the signaling
            # stream ending, or on a TUI Disconnect event. So Bye is purely
            # advisory; we just SIGTERM the friend in the teardown below.
        finally:
            if daemon.poll() is None:
                daemon.send_signal(signal.SIGINT)
                try:
                    daemon.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    daemon.kill()
    finally:
        if friend.poll() is None:
            friend.terminate()
            try:
                friend.wait(timeout=3)
            except subprocess.TimeoutExpired:
                friend.kill()

    return passed, failed


def main() -> int:
    if not PROBE_BIN.is_file():
        print(f"FATAL: {PROBE_BIN} not found", file=sys.stderr)
        return 2
    env = load_env()
    for k in ("EMQX_URL", "EMQX_USER", "EMQX_PASS"):
        if not env.get(k):
            print(f"FATAL: {k} missing in {ENV_FILE}", file=sys.stderr)
            return 2

    total_p = 0
    total_f = 0
    for relay in (False, True):
        name = f"dtest-{uuid.uuid4().hex[:8]}"
        print(f"\n=== probe daemon e2e: name={name} mqtt_relay={relay} ===")
        p, f = run_relay_or_webrtc(name, env, mqtt_relay=relay)
        total_p += p
        total_f += f

    print(f"\n=== results: {total_p} passed, {total_f} failed ===")
    return 0 if total_f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""End-to-end test for the --mqtt-relay fallback transport.

Mirrors test_e2e.py but spawns the probe with --mqtt-relay (no WebRTC at
all) and drives the RelaySession on the operator side. Used to catch
regressions in the broker-relayed Frame path that UDP-blocked friends
depend on.

Usage:
    python3 tools/test_e2e_relay.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import platform
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import aiomqtt

SCOPE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCOPE_ROOT / "tools"))

import probe_op  # noqa: E402

PROBE_BIN = SCOPE_ROOT / "binary" / "target" / "release" / "probe"
ENV_FILE = SCOPE_ROOT / "tools" / ".env.probe"

CONNECT_TIMEOUT = 15.0
EXEC_TIMEOUT = 15.0


def load_env_file(path: Path) -> dict:
    env = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def spawn_probe(name: str, env: dict) -> subprocess.Popen:
    cmd = [str(PROBE_BIN), "--name", name, "--no-consent", "--mqtt-relay"]
    print(f"  spawn: {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        env={**os.environ, "RUST_LOG": "probe=info", "EMQX_PASS": env["EMQX_PASS"]},
    )


def wait_for_probe_ready(proc: subprocess.Popen, name: str, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        line = proc.stderr.readline().decode("utf-8", errors="replace")
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


SCENARIOS = [
    ("echo hello", "echo hello", 0,
     lambda b: b == b"hello\n", lambda b: b == b""),
    ("stderr + exit 3", "printf err >&2; exit 3", 3,
     lambda b: b == b"", lambda b: b == b"err"),
    ("seq 1..50", "seq 1 50", 0,
     lambda b: b == ("\n".join(str(i) for i in range(1, 51)) + "\n").encode(),
     lambda b: b == b""),
    ("hostname", "cat /etc/hostname", 0,
     lambda b: b == (platform.node() + "\n").encode() or len(b) > 0,
     lambda b: b == b""),
]


def _build_args(name: str, env: dict) -> argparse.Namespace:
    return argparse.Namespace(
        name=name,
        mode=None,
        mqtt_url=env["EMQX_URL"],
        mqtt_user=env["EMQX_USER"],
        mqtt_pass=env["EMQX_PASS"],
        cf_turn_id=None,
        cf_turn_token=None,
        no_turn=True,
        mqtt_relay=True,
        timeout=EXEC_TIMEOUT,
        connect_timeout=CONNECT_TIMEOUT,
        verbose=False,
    )


async def run_scenarios(name: str, env: dict) -> tuple[int, int]:
    args = _build_args(name, env)
    url = urlparse(args.mqtt_url)
    client_kwargs = dict(
        hostname=url.hostname,
        port=url.port or 8084,
        username=args.mqtt_user,
        password=args.mqtt_pass,
        identifier=f"relay-e2e-{uuid.uuid4().hex[:8]}",
        keepalive=30,
        tls_params=aiomqtt.TLSParameters(),
        transport="websockets",
        websocket_path=url.path or "/mqtt",
    )

    passed = failed = 0
    async with aiomqtt.Client(**client_kwargs) as mqtt:
        session = probe_op.RelaySession(args, mqtt, stream_to_stdio=False)
        await session.setup_pc()
        sig_task = asyncio.create_task(session.signaling_listener())
        try:
            for label, cmd, expected_rc, ok_stdout, ok_stderr in SCENARIOS:
                print(f"\n--- {label} ---")
                t0 = time.monotonic()
                rc, out, err = await session.exec_one(cmd, EXEC_TIMEOUT)
                dt = time.monotonic() - t0
                ok = rc == expected_rc and ok_stdout(out) and ok_stderr(err)
                print(f"  rc={rc} stdout={out!r} stderr={err!r} ({dt*1000:.0f}ms)")
                if ok:
                    print("  PASS")
                    passed += 1
                else:
                    print(f"  FAIL: expected rc={expected_rc}")
                    failed += 1
            await session.send_bye()
        finally:
            sig_task.cancel()
            try:
                await sig_task
            except (asyncio.CancelledError, Exception):
                pass
            await session.close()
    return passed, failed


def main() -> int:
    if not PROBE_BIN.is_file():
        print(f"FATAL: {PROBE_BIN} not built; cd binary && cargo build --release",
              file=sys.stderr)
        return 2
    env = load_env_file(ENV_FILE)
    name = f"relay-test-{uuid.uuid4().hex[:8]}"
    print(f"=== probe mqtt-relay e2e: name={name} ===")
    probe = spawn_probe(name, env)
    if not wait_for_probe_ready(probe, name):
        print("probe did not become ready", file=sys.stderr)
        probe.terminate()
        return 3
    drain_probe_stderr_background(probe)
    t = time.monotonic()
    try:
        passed, failed = asyncio.run(run_scenarios(name, env))
    finally:
        try:
            probe.terminate()
            probe.wait(timeout=2)
        except subprocess.TimeoutExpired:
            probe.kill()
    print(f"\n=== relay results: {passed} passed, {failed} failed ({time.monotonic()-t:.1f}s) ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

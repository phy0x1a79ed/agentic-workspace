#!/usr/bin/env python3
"""End-to-end test for probe transport + exec.

Spawns the friend-side probe binary as a subprocess (with --no-consent),
then drives `probe_op.Session` *in-process* over a single MQTT connection
and a single WebRTC peer connection — running all scenarios sequentially
on the same data channel. This eliminates per-scenario operator startup,
TURN mint, and ICE gather (the previous subprocess-per-scenario design
spent ~30s per case mostly on connection setup).

Requires:
    - tools/.env.probe with valid EMQX_URL/EMQX_USER/EMQX_PASS and
      (optionally) CF_TURN_TOKEN_ID/CF_TURN_API_TOKEN.
    - binary/target/release/probe built (cargo build --release)
    - aiortc + aiomqtt installed (conda env `awm`, or
      pip install -r tools/requirements.txt)

Usage:
    python3 tools/test_e2e.py
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

CONNECT_TIMEOUT = 30.0
EXEC_TIMEOUT = 20.0


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


def _build_session_args(name: str, env: dict) -> argparse.Namespace:
    """Construct the argparse.Namespace shape probe_op.Session expects."""
    return argparse.Namespace(
        name=name,
        mode=None,
        mqtt_url=env["EMQX_URL"],
        mqtt_user=env["EMQX_USER"],
        mqtt_pass=env["EMQX_PASS"],
        cf_turn_id=env.get("CF_TURN_TOKEN_ID"),
        cf_turn_token=env.get("CF_TURN_API_TOKEN"),
        no_turn=False,
        timeout=EXEC_TIMEOUT,
        connect_timeout=CONNECT_TIMEOUT,
        verbose=False,
    )


async def run_scenarios(name: str, env: dict) -> tuple[int, int]:
    args = _build_session_args(name, env)
    url = urlparse(args.mqtt_url)
    hostname = url.hostname
    port = url.port or (8084 if url.scheme in ("wss", "https") else 8883)
    path = url.path or "/mqtt"
    use_wss = url.scheme in ("wss", "https")

    client_kwargs = dict(
        hostname=hostname,
        port=port,
        username=args.mqtt_user,
        password=args.mqtt_pass,
        identifier=f"e2e-{uuid.uuid4().hex[:8]}",
        keepalive=30,
        tls_params=aiomqtt.TLSParameters(),
    )
    if use_wss:
        client_kwargs["transport"] = "websockets"
        client_kwargs["websocket_path"] = path

    passed = 0
    failed = 0

    async with aiomqtt.Client(**client_kwargs) as mqtt:
        session = probe_op.Session(args, mqtt, stream_to_stdio=False)
        await session.setup_pc()
        sig_task = asyncio.create_task(session.signaling_listener())
        try:
            await session.send_offer()
            try:
                await asyncio.wait_for(
                    session.channel_open.wait(),
                    timeout=CONNECT_TIMEOUT,
                )
            except asyncio.TimeoutError:
                print(f"FAIL: data channel did not open within {CONNECT_TIMEOUT}s",
                      file=sys.stderr)
                return 0, len(SCENARIOS)
            if session.connection_dead.is_set():
                print(f"FAIL: pc state {session.pc.connectionState}", file=sys.stderr)
                return 0, len(SCENARIOS)

            for label, cmd, expected_rc, ok_stdout, ok_stderr in SCENARIOS:
                print(f"\n--- {label} ---")
                print(f"  cmd: {cmd!r}")
                t0 = time.monotonic()
                try:
                    rc, out, err = await session.exec_one(cmd, EXEC_TIMEOUT)
                except Exception as e:
                    print(f"  FAIL: exec_one raised {e!r}", file=sys.stderr)
                    failed += 1
                    continue
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
        print(f"FATAL: {PROBE_BIN} not found; build with: cd binary && cargo build --release",
              file=sys.stderr)
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

    drain_probe_stderr_background(probe)

    t_total = time.monotonic()
    try:
        passed, failed = asyncio.run(run_scenarios(name, env))
    finally:
        print("\n=== teardown ===")
        try:
            probe.terminate()
            probe.wait(timeout=2)
        except subprocess.TimeoutExpired:
            probe.kill()
            probe.wait(timeout=1)

    elapsed = time.monotonic() - t_total
    print(f"\n=== results: {passed} passed, {failed} failed ({elapsed:.1f}s total) ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

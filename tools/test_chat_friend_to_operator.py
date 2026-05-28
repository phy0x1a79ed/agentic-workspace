#!/usr/bin/env python3
"""Chat-from-friend regression test for the on_close-clears-slot fix.

Drives the Rust probe's TUI chat composer via tmux send-keys, observes the
operator daemon's stderr log for a `[friend] <msg>` line, and then cycles
the daemon (graceful stop + fresh start against the same long-lived friend)
to verify the regression in `binary/src/transport.rs` is gone.

To confirm this test would catch a future regression: in
`binary/src/transport.rs`, replace the on_close clear guard
    slot.as_ref().map(|s| Arc::ptr_eq(s, &dc)).unwrap_or(false)
with `false` (or revert the on_close clear entirely), rebuild, rerun this
script — the second `tmux send-keys` phase will time out because the friend
keeps a stale closed-DC handle and `send_chat` fails on it.

Requires:
    - tools/.env.probe (EMQX creds)
    - binary/target/release/probe built
    - tmux on PATH
    - mamba env `awm` with aiortc + aiomqtt
"""
from __future__ import annotations

import os
import shutil
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

FRIEND_READY_TIMEOUT = 25.0
DAEMON_READY_TIMEOUT = 30.0
CHAT_DELIVERY_TIMEOUT = 12.0
SEND_TIMEOUT = 20.0


def load_env() -> dict:
    env = {}
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, check=check, text=True)


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


def spawn_friend_tmux(session: str, name: str, env: dict) -> None:
    """tmux gives the probe a pty so its TUI initializes — without that the
    TUI bails ('TUI setup failed: No such device or address') and chat-send
    is never wired up on the friend side."""
    cmd = (
        f"{PROBE_BIN} --name {name} --no-consent "
        f"--mqtt-pass '{env['EMQX_PASS']}'"
    )
    full_env = {
        **os.environ,
        "RUST_LOG": "probe=info",
        "EMQX_PASS": env["EMQX_PASS"],
    }
    # tmux's `-x`/`-y` keep capture-pane width predictable for substring search.
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-x", "200", "-y", "60", cmd],
        check=True,
        env=full_env,
    )


def friend_ready(session: str) -> bool:
    txt = pane_text(session)
    # The Rust binary's signaling::connect prints this via TuiMsg::System
    # once the from-operator/+ subscription lands (binary/src/main.rs:136-138).
    return "subscribed; waiting for operator" in txt


def spawn_daemon(name: str, log_path: Path, env: dict) -> subprocess.Popen:
    log_path.write_text("")  # truncate
    log_fh = open(log_path, "ab", buffering=0)
    full_env = {
        **os.environ,
        **env,
        "PYTHONUNBUFFERED": "1",
    }
    return subprocess.Popen(
        [sys.executable, "-u", str(OPERATOR), name, "daemon"],
        stdout=log_fh,
        stderr=log_fh,
        stdin=subprocess.DEVNULL,
        env=full_env,
    )


def wait_socket(name: str, proc: subprocess.Popen, timeout: float) -> Path | None:
    path = probe_op._socket_path(name)
    if wait_for(lambda: path.exists() or proc.poll() is not None, timeout):
        return path if path.exists() else None
    return None


def run_send(name: str, cmd: str, env: dict, timeout: float = SEND_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(OPERATOR), name, "send", cmd],
        capture_output=True,
        env={**os.environ, **env},
        timeout=timeout,
    )


def run_stop(name: str, env: dict) -> int:
    proc = subprocess.run(
        [sys.executable, str(OPERATOR), name, "stop"],
        capture_output=True,
        env={**os.environ, **env},
        timeout=10.0,
    )
    return proc.returncode


def assert_chat_arrives(session: str, log_path: Path, payload: str) -> bool:
    """Type `payload` into the friend's TUI composer (Enter ships it) and
    wait for `[friend] payload` to appear in the daemon log.

    `-l` forces literal-string mode so tmux doesn't try to parse the
    payload as a key name (hyphens, etc.). Enter is sent as a separate
    key-name press, after a brief sleep so the probe's 50 ms-tick event
    poll has time to drain the typed chars into the composer before Enter
    arrives — without that gap, the probe occasionally sees an empty
    composer at Enter time and `msg.is_empty()` swallows the send."""
    tmux("send-keys", "-t", session, "-l", payload)
    # Brief pause so the probe's 50 ms-tick TUI poll has time to drain the
    # typed chars into the composer before Enter arrives — without it the
    # probe occasionally sees an empty composer at Enter time and
    # `msg.is_empty()` swallows the send.
    time.sleep(0.4)
    tmux("send-keys", "-t", session, "Enter")

    needle = f"[friend] {payload}"

    def found():
        try:
            return needle in log_path.read_text(errors="replace")
        except FileNotFoundError:
            return False

    return wait_for(found, CHAT_DELIVERY_TIMEOUT)


def cleanup(session: str | None, daemons: list[subprocess.Popen], name: str, env: dict, log_paths: list[Path]) -> None:
    # Graceful daemon stop first (gives them a chance to publish Bye)
    if name:
        try:
            run_stop(name, env)
        except Exception:
            pass
    for d in daemons:
        try:
            if d.poll() is None:
                d.send_signal(signal.SIGTERM)
                try:
                    d.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    d.kill()
                    d.wait(timeout=2)
        except Exception:
            pass
    if session:
        try:
            tmux("kill-session", "-t", session, check=False)
        except Exception:
            pass
    try:
        sock = probe_op._socket_path(name)
        sock.unlink(missing_ok=True)
    except Exception:
        pass
    for p in log_paths:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


def main() -> int:
    if not PROBE_BIN.is_file():
        print(f"FATAL: {PROBE_BIN} not found; run `cargo build --release` in binary/", file=sys.stderr)
        return 2
    if shutil.which("tmux") is None:
        print("FATAL: tmux not on PATH; this test drives the friend's TUI via tmux send-keys", file=sys.stderr)
        return 2
    env = load_env()
    for k in ("EMQX_URL", "EMQX_USER", "EMQX_PASS"):
        if not env.get(k):
            print(f"FATAL: {k} missing in {ENV_FILE}", file=sys.stderr)
            return 2

    name = f"chat-{uuid.uuid4().hex[:8]}"
    session = f"probe-chat-{uuid.uuid4().hex[:8]}"
    log_a = Path(f"/tmp/test-chat-{name}-A.log")
    log_b = Path(f"/tmp/test-chat-{name}-B.log")

    daemons: list[subprocess.Popen] = []
    passed = 0
    failed = 0

    print(f"=== test_chat_friend_to_operator: name={name} ===")

    try:
        # ---- friend (long-lived across daemon cycles) ----
        spawn_friend_tmux(session, name, env)
        if not wait_for(lambda: friend_ready(session), FRIEND_READY_TIMEOUT):
            print(f"FAIL: friend did not become ready in {FRIEND_READY_TIMEOUT}s")
            print(f"--- last pane snapshot ---\n{pane_text(session)}")
            return 1
        print("  friend ready (tmux pty)")

        # ---- daemon A ----
        d_a = spawn_daemon(name, log_a, env)
        daemons.append(d_a)
        if wait_socket(name, d_a, DAEMON_READY_TIMEOUT) is None:
            print("FAIL: daemon A socket never appeared")
            print(f"--- daemon A log ---\n{log_a.read_text(errors='replace')}")
            return 1
        print("  daemon A up")

        # ---- smoke: exec works (rules out a generic channel break) ----
        r = run_send(name, "echo exec-ok", env)
        if r.returncode != 0 or r.stdout != b"exec-ok\n":
            print(f"FAIL: smoke send failed rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}")
            return 1
        passed += 1
        print("  PASS smoke exec")

        # ---- G2: chat from friend lands at daemon A ----
        if assert_chat_arrives(session, log_a, "hello-via-tui-1"):
            passed += 1
            print("  PASS G2 (friend chat → daemon A)")
        else:
            failed += 1
            print(f"FAIL G2: '[friend] hello-via-tui-1' never appeared in {log_a}")
            print(f"--- daemon A log tail ---\n{log_a.read_text(errors='replace')[-1500:]}")
            print(f"--- friend pane ---\n{pane_text(session)[-1500:]}")

        # ---- G3 setup: stop daemon A, start daemon B against same friend ----
        rc = run_stop(name, env)
        if rc != 0:
            failed += 1
            print(f"FAIL G3-setup: stop rc={rc}")
        try:
            d_a.wait(timeout=10)
        except subprocess.TimeoutExpired:
            failed += 1
            print("FAIL G3-setup: daemon A did not exit after stop")
            d_a.kill()
        # socket should be unlinked by the daemon's teardown
        sock_path = probe_op._socket_path(name)
        if not wait_for(lambda: not sock_path.exists(), 5.0):
            sock_path.unlink(missing_ok=True)

        d_b = spawn_daemon(name, log_b, env)
        daemons.append(d_b)
        if wait_socket(name, d_b, DAEMON_READY_TIMEOUT) is None:
            failed += 1
            print("FAIL G3: daemon B socket never appeared")
            print(f"--- daemon B log ---\n{log_b.read_text(errors='replace')}")
            return 1
        print("  daemon B up (same friend)")

        # smoke exec against B — separates a generic reconnect break from
        # the asymmetric chat-only break that this test is for.
        r = run_send(name, "echo exec-ok-B", env)
        if r.returncode != 0 or r.stdout != b"exec-ok-B\n":
            failed += 1
            print(f"FAIL G3-smoke: daemon B exec rc={r.returncode} stdout={r.stdout!r}")
            print(f"--- daemon B log ---\n{log_b.read_text(errors='replace')}")
            return 1
        print("  PASS daemon B exec")

        # ---- G3: chat after reconnect ----
        if assert_chat_arrives(session, log_b, "hello-via-tui-2"):
            passed += 1
            print("  PASS G3 (friend chat → daemon B after reconnect)")
        else:
            failed += 1
            print(f"FAIL G3: '[friend] hello-via-tui-2' never appeared in {log_b}")
            print(f"--- daemon B log tail ---\n{log_b.read_text(errors='replace')[-1500:]}")
            print(f"--- friend pane ---\n{pane_text(session)[-1500:]}")
    finally:
        cleanup(session, daemons, name, env, [log_a, log_b])

    print(f"\n=== results: {passed} passed, {failed} failed ===")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

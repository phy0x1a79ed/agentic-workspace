#!/usr/bin/env bash
# Run the full local test suite for the probe stack.
#
#   tools/test_local.sh
#
# Stages in order:
#   1. cargo build --release + cargo test
#   2. tools/test_e2e.py            (one-shot exec over WebRTC)
#   3. tools/test_e2e_relay.py      (one-shot exec over MQTT-relay)
#   4. tools/test_e2e_daemon.py     (persistent daemon + send)
#   5. tools/test_chat_friend_to_operator.py (chat composer + reconnect regression)
#   6. tools/test_repl_mode.py      (operator REPL mode)
#
# Each stage prints `=== stage N: <name> ===`. First failure aborts.
# The EXIT/INT/TERM trap reaps any leftover probe/probe_op.py/tmux state.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

STAGE=0

cleanup() {
  # Reap orphan probe processes and tmux sessions spawned by the tests.
  pkill -f "binary/target/release/probe" 2>/dev/null || true
  pkill -f "probe_op.py" 2>/dev/null || true
  # Kill only tmux sessions our tests create (prefix-namespaced).
  if command -v tmux >/dev/null 2>&1; then
    tmux ls -F '#{session_name}' 2>/dev/null \
      | grep -E '^(probe-chat-|probe-repl-)' \
      | xargs -r -I{} tmux kill-session -t {} 2>/dev/null || true
  fi
  # Remove any stale daemon sockets named probe-{*}-{uid}.sock under XDG_RUNTIME_DIR.
  uid=$(id -u)
  for d in "${XDG_RUNTIME_DIR:-/run/user/$uid}" /tmp; do
    [ -d "$d" ] || continue
    rm -f "$d"/probe-*-"$uid".sock 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

stage() {
  STAGE=$((STAGE + 1))
  echo
  echo "=== stage $STAGE: $1 ==="
}

run_py() {
  mamba run --no-capture-output -n awm python "$1"
}

require() {
  for t in "$@"; do
    command -v "$t" >/dev/null 2>&1 || { echo "FATAL: $t not on PATH" >&2; exit 2; }
  done
}

require mamba tmux

stage "build + cargo test"
( cd binary && cargo build --release && cargo test --quiet )

stage "tools/test_e2e.py"
run_py tools/test_e2e.py

if [ -f tools/.env.probe ] && grep -q '^EMQX_PASS=' tools/.env.probe; then
  stage "tools/test_e2e_relay.py"
  run_py tools/test_e2e_relay.py
else
  echo "skip: tools/.env.probe missing — relay test needs EMQX creds"
fi

stage "tools/test_e2e_daemon.py"
run_py tools/test_e2e_daemon.py

stage "tools/test_chat_friend_to_operator.py"
run_py tools/test_chat_friend_to_operator.py

stage "tools/test_repl_mode.py"
run_py tools/test_repl_mode.py

echo
echo "=== all local tests passed ==="

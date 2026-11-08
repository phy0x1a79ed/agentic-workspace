#!/usr/bin/env bash
# Dev sandbox: this worktree's Rust gateway, serving this worktree's plugins.
#
#   ./run.sh              # start (default)
#   ./run.sh start        # write the sandbox config, start the gateway
#   ./run.sh stop         # stop the gateway and the services it started
#   ./run.sh restart      # stop + start
#   ./run.sh reset        # wipe sandbox state and re-seed (asks first)
#   ./run.sh seed         # (re)seed the sandbox through /invoke
#   ./run.sh status       # what runs, at which URL
#   ./run.sh logs         # tail the gateway log; service logs are in .awm/logs/
#
# Also driven by the `dev` service: `awm dev start --worktree <abs path>`.
#
# State (.awm/, projects/, data/) lives next to this file and is gitignored.
# .awm/ is the gateway's state dir (AWM_DIR): run.sh rewrites its config.toml
# on every start. The gateway reloads a plugin when its files change
# (`awm reload`), so there is no shadow of prod: test against the sandbox port.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# core/plugins/dev/sandbox → up four to the worktree root.
REPO_ROOT="$(cd "$HERE/../../../.." && pwd)"

# Per-worktree overrides (gitignored), sourced before the port defaults so
# .env can pin AWM_PORT.
if [ -f "$HERE/.env" ]; then
  # shellcheck disable=SC1091
  set -a; . "$HERE/.env"; set +a
fi

# A port band per worktree, so several sandboxes run side by side.
case "$(basename "$(realpath "$REPO_ROOT")")" in
  dev)         _PORT_BASE=782 ;;
  web-ui)      _PORT_BASE=783 ;;
  web-backend) _PORT_BASE=784 ;;
  *)           _PORT_BASE=785 ;;
esac
AWM_PORT="${AWM_PORT:-${_PORT_BASE}1}"

STATE="$HERE/.awm"
PID_FILE="$STATE/dev.pid"
LOG_FILE="$STATE/dev.log"
BASE="http://127.0.0.1:${AWM_PORT}"
# The sandbox does not run the dev service: a sandbox would drive sandboxes.
DEV_EXCLUDE="${DEV_EXCLUDE:-dev}"

# The built binary named $1: $2 if set, then the worktree's cargo target, then PATH.
_built() {
  local name="$1" pick="$2" b target
  target="$("$REPO_ROOT/scripts/rust-build.sh" --target-dir)"
  for b in "$pick" "$target/release/$name" "$target/debug/$name" "$target/x86_64-unknown-linux-musl/release/$name"; do
    [ -n "$b" ] && [ -x "$b" ] && { printf '%s' "$b"; return 0; }
  done
  command -v "$name"
}

_dev_bin() {
  _built dev "${DEV_BIN:-}" || { echo "[dev] no dev binary: run $REPO_ROOT/scripts/rust-build.sh build -p awm-plugin-dev" >&2; return 1; }
}

# Each core plugin's run.sh execs $AWM_DIR/bin/<name>, so link this worktree's build of each
# into the sandbox's bin. A plugin not built stays down, and its log says so.
_link_plugins() {
  local dir name bin
  for dir in "$REPO_ROOT"/core/plugins/*/; do
    name="$(basename "$dir")"
    [ "$name" = dev ] && continue
    if bin="$(_built "$name" "" 2>/dev/null)"; then
      ln -sfn "$bin" "$STATE/bin/$name"
    else
      echo "[dev] $name is not built: run $REPO_ROOT/scripts/rust-build.sh build -p awm-plugin-$name" >&2
    fi
  done
}

_awm_bin() {
  local b target
  target="$("$REPO_ROOT/scripts/rust-build.sh" --target-dir)"
  for b in "${AWM_BIN:-}" "$target/release/awm" "$target/debug/awm"; do
    [ -n "$b" ] && [ -x "$b" ] && { printf '%s' "$b"; return 0; }
  done
  command -v awm || { echo "[dev] no awm binary: run $REPO_ROOT/scripts/rust-build.sh build" >&2; return 1; }
}

# Every dist root of the worktree, so each service the sandbox starts imports
# this worktree's code. A dir is a dist root iff it holds an awm/ namespace dir.
# It reaches the services as PYTHONPATH with AWM_PYTHON, which each run.sh
# execs. `mamba run --no-capture-output` is no way in: mamba 2.5 breaks it (see
# scripts/run-tests.sh).
_pythonpath() {
  local d pp=""
  for d in "$REPO_ROOT"/awm/service_components/* "$REPO_ROOT"/awm/services/* "$REPO_ROOT"/awm/plugins/*; do
    [ -d "$d/awm" ] || continue
    pp="${pp:+$pp:}$d"
  done
  printf '%s' "$pp"
}

# The awm env's interpreter. `mamba run` appends a blank line on some builds.
_python() {
  local py
  local mamba
  mamba="$(command -v mamba || ls "$HOME"/miniforge3/bin/mamba "$HOME"/lib/miniforge3/bin/mamba 2>/dev/null | head -1)"
  py="$("${mamba:-mamba}" run -n "${AWM_ENV:-awm}" python -c 'import sys; print(sys.executable)' 2>/dev/null | grep -v '^[[:space:]]*$' | tail -1)"
  [ -x "$py" ] || { echo "[dev] could not resolve the ${AWM_ENV:-awm} env's python" >&2; return 1; }
  printf '%s' "$py"
}

# The names .env sets, minus those the [env] block writes itself, so every
# other .env key, such as DSH_FORK_DIR, reaches the services.
_env_names() {
  [ -f "$HERE/.env" ] || return 0
  sed -nE 's/^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=.*/\2/p' "$HERE/.env" \
    | grep -vxE 'PYTHONPATH|AWM_PYTHON|AWM_ENV_BIN|AWM_ALLOW_DESTRUCTIVE|AWM_GITHUB_USER|GIT_CONFIG_(COUNT|KEY_0|VALUE_0)' \
    | sort -u
}

_toml_env() {
  local name value
  for name in $(_env_names); do
    value="${!name-}"
    value="${value//\\/\\\\}"
    printf '%s = "%s"\n' "$name" "${value//\"/\\\"}"
  done
}

_toml_list() {
  local out="" x
  for x in "$@"; do out="${out:+$out, }\"$x\""; done
  printf '[%s]' "$out"
}

write_config() {
  mkdir -p "$STATE/bin" "$STATE/services"
  # No real GitHub repos from the sandbox: scopes shells out to `gh` when it is on PATH.
  if [ ! -x "$STATE/bin/gh" ]; then
    printf '#!/usr/bin/env bash\nexit 0\n' >"$STATE/bin/gh"
    chmod +x "$STATE/bin/gh"
  fi
  _link_plugins
  local py
  py="$(_python)"
  # shellcheck disable=SC2086
  cat >"$STATE/config.toml" <<EOF
# Written by run.sh on every start; edit .env instead.
node = "$(hostname -s)-dev"
listen = "127.0.0.1:${AWM_PORT}"
workspace = "$HERE"
core_roots = $(_toml_list "$REPO_ROOT/core/plugins" "$REPO_ROOT/awm/services" "$REPO_ROOT/awm/plugins")
exclude = $(_toml_list $DEV_EXCLUDE)

[env]
PYTHONPATH = "$(_pythonpath)"
AWM_PYTHON = "$py"
AWM_ENV_BIN = "$(dirname "$py")"
AWM_ALLOW_DESTRUCTIVE = "1"
AWM_GITHUB_USER = "${AWM_GITHUB_USER:-dev-sandbox}"
# git init --bare honours the operator's init.defaultBranch; scopes needs main.
GIT_CONFIG_COUNT = "1"
GIT_CONFIG_KEY_0 = "init.defaultBranch"
GIT_CONFIG_VALUE_0 = "main"
$(_toml_env)
EOF
}

is_running() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

# One "<name> <status> <pid>" line per service the gateway runs, from `awm services`.
_services() {
  "$(_dev_bin)" sandbox services --port "$AWM_PORT" 2>/dev/null
}

_port_listeners() {
  ss -tlnp 2>/dev/null | awk -v p=":$AWM_PORT" '$4 ~ p"$"' | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
}

do_stop() {
  local pids pid stopped=0
  # Services run in their own process groups, so stopping the gateway alone
  # can leave them retrying. Read their pids before the gateway goes.
  pids="$(_services | awk '$3 != "-" {print $3}' || true)"
  if is_running; then
    pid="$(cat "$PID_FILE")"
    echo "[dev] stopping gateway pid $pid"
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 25); do kill -0 "$pid" 2>/dev/null || break; sleep 0.2; done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    stopped=1
  fi
  rm -f "$PID_FILE"
  for pid in $pids; do
    kill -0 "$pid" 2>/dev/null || continue
    echo "[dev] stopping service pid group $pid"
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    stopped=1
  done
  # A gateway that lost its pid file still holds the port.
  pids="$(_port_listeners || true)"
  if [ -n "$pids" ]; then
    echo "[dev] stopping straggler(s) on :$AWM_PORT: $(echo "$pids" | tr '\n' ' ')"
    # shellcheck disable=SC2086
    kill -TERM $pids 2>/dev/null || true
    stopped=1
  fi
  [ "$stopped" = 1 ] || echo "[dev] not running"
}

_wait_up() {
  local deadline=$(( SECONDS + 30 ))
  while [ "$SECONDS" -lt "$deadline" ]; do
    curl -s -m2 "$BASE/status" >/dev/null 2>&1 && return 0
    is_running || break
    sleep 0.5
  done
  echo "[dev] gateway did not come up — see $LOG_FILE"
  tail -n 40 "$LOG_FILE" || true
  return 1
}

do_start() {
  if is_running; then
    echo "[dev] already running (pid $(cat "$PID_FILE")) — use restart"
    echo "[dev]   $BASE/"
    return 0
  fi
  local bin
  bin="$(_awm_bin)"
  write_config
  echo "[dev] gateway  $BASE/  ($bin)"
  PATH="$STATE/bin:$PATH" AWM_DIR="$STATE" \
    nohup setsid "$bin" gateway serve --state "$STATE" >"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  _wait_up || { do_stop >/dev/null; exit 1; }
  echo "[dev] gateway up — services starting; check: AWM_DIR=$STATE awm services"
  echo "[dev] logs: gateway=$LOG_FILE services=$STATE/logs/"
}

do_status() {
  if ! is_running; then
    echo "[dev] not running"
    return 0
  fi
  echo "[dev] gateway       running (pid $(cat "$PID_FILE"))"
  echo "[dev]   $BASE/"
  _services | awk '{printf "[dev]   %-16s %s\n", $1, $2}'
}

do_seed() {
  echo "[dev] seeding sandbox"
  AWM_WORKSPACE="$HERE" "$(_dev_bin)" sandbox seed --port "$AWM_PORT" --sandbox "$HERE"
}

do_reset() {
  is_running && do_stop
  read -r -p "[dev] wipe $HERE/.awm $HERE/projects $HERE/data ? [y/N] " ans
  case "$ans" in
    y|Y|yes|YES)
      rm -rf "$HERE/.awm" "$HERE/projects" "$HERE/data"
      echo "[dev] wiped."
      do_start
      do_seed
      ;;
    *) echo "[dev] aborted."; exit 1 ;;
  esac
}

case "${1:-start}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; do_start ;;
  status)  do_status ;;
  seed)    do_seed ;;
  reset)   do_reset ;;
  logs)    tail -n 200 -F "$LOG_FILE" ;;
  *) echo "usage: $0 {start|stop|restart|status|seed|reset|logs}"; exit 2 ;;
esac

#!/usr/bin/env bash
# Dev harness for the web-ui.
#
#   ./run.sh              # start (default)
#   ./run.sh start        # start uvicorn
#   ./run.sh stop         # stop uvicorn
#   ./run.sh restart      # stop + start
#   ./run.sh reset        # wipe sandbox state and re-seed (asks first)
#   ./run.sh seed         # (re)seed the sandbox DB without restarting
#   ./run.sh status       # what's running, what URLs
#   ./run.sh logs         # tail the uvicorn log
#
# State lives in this directory (.awm/, projects/, data/) and is gitignored.
# Backend Python reloads via uvicorn --reload; static HTML/JS reloads on
# browser refresh (no build step).
#
# uvicorn binds http://127.0.0.1:7821 on loopback (federation/TLS/auth retired).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

# Per-worktree env overrides (gitignored). Sourced before computing port
# defaults so .env can pin AWM_PORT etc. without env exports.
if [ -f "$HERE/.env" ]; then
  # shellcheck disable=SC1091
  set -a; . "$HERE/.env"; set +a
fi

# Scope-aware default port band — each worktree (dev/web-ui/web-backend/…)
# gets a unique band so three sandboxes can run side-by-side. Override by
# setting AWM_PORT / VITE_PORT explicitly (env or dev/.env).
case "$(basename "$(realpath "$REPO_ROOT")")" in
  dev)         _PORT_BASE=782; _VITE_PORT=12103 ;;
  web-ui)      _PORT_BASE=783; _VITE_PORT=12113 ;;
  web-backend) _PORT_BASE=784; _VITE_PORT=12123 ;;
  *)           _PORT_BASE=785; _VITE_PORT=12153 ;;
esac

PID_FILE="$HERE/.awm/dev.pid"
LOG_FILE="$HERE/.awm/dev.log"
SYNC_PID_FILE="$HERE/.awm/stripe-sync.pid"
SYNC_LOG_FILE="$HERE/.awm/stripe-sync.log"

export AWM_WORKSPACE="$HERE"
export AWM_PORT="${AWM_PORT:-${_PORT_BASE}1}"
export VITE_PORT="${VITE_PORT:-$_VITE_PORT}"
export AWM_ALLOW_DESTRUCTIVE=1
export AWM_IDLE_SHUTDOWN=999999
export AWM_GITHUB_USER="${AWM_GITHUB_USER:-dev-sandbox}"

# Two production-code quirks that the dev sandbox has to work around. We
# park the fixes here (on the uvicorn process) so seed.py can stay a thin
# HTTP client — see seed.py for the rationale.
#
#   1. awm.services.projects.create_project shells out to `gh repo create`
#      if `gh` is on PATH. We don't want the sandbox creating real GitHub
#      repos, so we put a no-op `gh` shim earlier on PATH.
#   2. `git init --bare` honours the operator's init.defaultBranch; if it
#      isn't `main` the subsequent worktree-add fails with "Not a valid
#      object name: 'HEAD'". Forcing it here via env keeps it process-local.
SHIM_DIR="$HERE/.awm/bin"
mkdir -p "$SHIM_DIR"
if [ ! -x "$SHIM_DIR/gh" ]; then
  printf '#!/usr/bin/env bash\nexit 0\n' >"$SHIM_DIR/gh"
  chmod +x "$SHIM_DIR/gh"
fi
export PATH="$SHIM_DIR:$PATH"
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=init.defaultBranch
export GIT_CONFIG_VALUE_0=main

URL="http://127.0.0.1:${AWM_PORT}/ui/"

cmd="${1:-start}"

is_running_pidfile() {
  local pf="$1"
  [ -f "$pf" ] && kill -0 "$(cat "$pf")" 2>/dev/null
}
is_running()       { is_running_pidfile "$PID_FILE"; }
is_sync_running()  { is_running_pidfile "$SYNC_PID_FILE"; }

prep() {
  mkdir -p "$HERE/.awm"
  (cd "$REPO_ROOT" && mamba run -n awm --no-capture-output \
      python "$HERE/_prep.py" "$@")
}

_port_listeners() {
  # PIDs listening on $1 (default $AWM_PORT).
  local port="${1:-$AWM_PORT}"
  ss -tlnp 2>/dev/null \
    | awk -v p=":$port" '$4 ~ p' \
    | grep -oE 'pid=[0-9]+' \
    | cut -d= -f2 \
    | sort -u
}

_stop_one() {
  # _stop_one <pid_file> <port> <label>
  local pf="$1" port="$2" label="$3"
  local stopped=0
  if [ -f "$pf" ]; then
    local pid
    pid="$(cat "$pf")"
    if kill -0 "$pid" 2>/dev/null; then
      echo "[dev] stopping $label pid $pid (and descendants)"
      pkill -TERM -P "$pid" 2>/dev/null || true
      kill -TERM "$pid" 2>/dev/null || true
      stopped=1
    fi
    rm -f "$pf"
  fi

  # mamba run wraps the child in a subprocess tree that can re-parent to
  # init when its launcher exits, so the saved pid alone isn't always
  # enough. Sweep anything still bound to the port.
  local extras
  extras="$(_port_listeners "$port" || true)"
  if [ -n "$extras" ]; then
    echo "[dev] stopping $label straggler(s) on :$port: $(echo "$extras" | tr '\n' ' ')"
    # shellcheck disable=SC2086
    kill -TERM $extras 2>/dev/null || true
    stopped=1
  fi
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    extras="$(_port_listeners "$port" || true)"
    [ -z "$extras" ] && break
    sleep 0.2
  done
  extras="$(_port_listeners "$port" || true)"
  if [ -n "$extras" ]; then
    echo "[dev] SIGKILL on $label: $extras"
    # shellcheck disable=SC2086
    kill -9 $extras 2>/dev/null || true
  fi
  echo "$stopped"
}

_stop_sync() {
  # The stripe-sync process holds N WS leases; SIGTERM is enough — closing
  # the leases triggers hub eviction (and SIGTERMs the supervised backends).
  # It does not bind a port, so the port-based straggler sweep in _stop_one
  # doesn't apply.
  if [ -f "$SYNC_PID_FILE" ]; then
    local pid
    pid="$(cat "$SYNC_PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
      echo "[dev] stopping stripe-sync pid $pid"
      pkill -TERM -P "$pid" 2>/dev/null || true
      kill -TERM "$pid" 2>/dev/null || true
    fi
    rm -f "$SYNC_PID_FILE"
  fi
}

do_stop() {
  local s1
  # Stop stripe-sync first so leases close cleanly before the hub goes
  # down (hub-side eviction terminates supervised backends).
  _stop_sync
  s1="$(_stop_one "$PID_FILE" "$AWM_PORT" "uvicorn")"
  if [ "$s1" = "0" ]; then
    echo "[dev] not running"
  fi
}

do_seed() {
  prep
  echo "[dev] seeding sandbox"
  (cd "$REPO_ROOT" && mamba run -n awm --no-capture-output \
      python "$HERE/seed.py")
}

_start_uvicorn() {
  echo "[dev] uvicorn  $URL"
  cd "$REPO_ROOT"
  nohup setsid mamba run -n awm --no-capture-output \
      uvicorn awm.server:app \
      --host 127.0.0.1 --port "$AWM_PORT" \
      --reload --reload-dir "$REPO_ROOT/awm" \
      >"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  sleep 1
  if ! is_running; then
    echo "[dev] uvicorn failed to start — see $LOG_FILE"
    tail -n 40 "$LOG_FILE" || true
    rm -f "$PID_FILE"
    exit 1
  fi
}

NODE_BIN="/home/tony/lib/miniforge3/envs/awm/bin"

_build_packages() {
  # Layout-driven build pipeline:
  #   1. awm packages gen — write generated package.json + per-page
  #      vite.config.ts from the packages/{components,pages}/<name>/
  #      layout + a src/ import scan (idempotent).
  #   2. npm install — symlink workspace packages.
  #   3. npm run build --workspaces --if-present — pages with a build
  #      script (every generated page) emit dist/.
  local pkgs_root="$REPO_ROOT/packages"
  local root_pj="$REPO_ROOT/package.json"
  if [ ! -d "$pkgs_root" ] || [ ! -f "$root_pj" ]; then
    return 0
  fi
  echo "[dev] awm packages gen $REPO_ROOT"
  (cd "$REPO_ROOT" && mamba run -n awm --no-capture-output \
      python -m awm packages gen "$REPO_ROOT") \
    >>"$SYNC_LOG_FILE" 2>&1 || {
      echo "[dev] manifest gen failed — see $SYNC_LOG_FILE"
      return 1
    }
  if [ ! -d "$REPO_ROOT/node_modules" ]; then
    echo "[dev] npm install (workspace root)…"
    (cd "$REPO_ROOT" && PATH="$NODE_BIN:$PATH" npm install --no-audit --no-fund) \
      >>"$SYNC_LOG_FILE" 2>&1 || {
        echo "[dev] npm install failed — see $SYNC_LOG_FILE"
        return 1
      }
  fi
  echo "[dev] npm run build --workspaces --if-present"
  (cd "$REPO_ROOT" && PATH="$NODE_BIN:$PATH" \
      npm run build --workspaces --if-present) \
    >>"$SYNC_LOG_FILE" 2>&1 || {
      echo "[dev] workspace build failed — see $SYNC_LOG_FILE"
      return 1
    }
}

_start_stripe_sync() {
  # Walk packages/{services,pages}/<name>/ and register each with the
  # hub via `awm packages sync`. Services are spawned via start.sh +
  # self-register over a control WS; pages register as kind=page at
  # /ui/<name>. If nothing is present, silently skip.
  local pkgs_root="$REPO_ROOT/packages"
  if [ ! -d "$pkgs_root" ]; then
    return 0
  fi
  if ! find "$pkgs_root/services" "$pkgs_root/pages" \
        -maxdepth 2 \( -name start.sh -o -name dist \) -print -quit \
        2>/dev/null | grep -q .; then
    return 0
  fi
  _build_packages || return 1
  echo "[dev] packages-sync  $REPO_ROOT/packages/"
  cd "$REPO_ROOT"
  nohup setsid mamba run -n awm --no-capture-output \
      python -m awm packages sync "$REPO_ROOT" \
      >"$SYNC_LOG_FILE" 2>&1 &
  echo $! >"$SYNC_PID_FILE"
  sleep 0.5
  if ! is_sync_running; then
    echo "[dev] packages-sync failed to start — see $SYNC_LOG_FILE"
    tail -n 20 "$SYNC_LOG_FILE" || true
    rm -f "$SYNC_PID_FILE"
  fi
}

do_start() {
  if is_running; then
    echo "[dev] already running (pid $(cat "$PID_FILE")) — use restart"
    echo "[dev]   $URL"
    [ -f "$LOGIN_PID_FILE" ] && echo "[dev]   $LOGIN_URL"
    exit 0
  fi
  prep
  _start_uvicorn
  _start_stripe_sync
  echo "[dev] logs: $LOG_FILE"
}

do_status() {
  local any=0
  if is_running; then
    echo "[dev] uvicorn       running (pid $(cat "$PID_FILE"))"
    echo "[dev]   $URL"
    any=1
  fi
  if is_sync_running; then
    echo "[dev] stripe-sync   running (pid $(cat "$SYNC_PID_FILE"))"
    any=1
  fi
  [ "$any" -eq 0 ] && echo "[dev] not running"
}

do_reset() {
  if is_running; then
    do_stop
  fi
  read -r -p "[dev] wipe $HERE/.awm $HERE/projects $HERE/data ? [y/N] " ans
  case "$ans" in
    y|Y|yes|YES)
      rm -rf "$HERE/.awm" "$HERE/projects" "$HERE/data"
      echo "[dev] wiped."
      do_seed
      ;;
    *)
      echo "[dev] aborted."
      exit 1
      ;;
  esac
}

case "$cmd" in
  start)    do_start ;;
  stop)     do_stop ;;
  restart)  do_stop; do_start ;;
  status)   do_status ;;
  seed)     do_seed ;;
  reset)    do_reset ;;
  logs)     tail -n 200 -F "$LOG_FILE" ;;
  *)
    echo "usage: $0 {start|stop|restart|status|seed|reset|logs}"
    exit 2
    ;;
esac

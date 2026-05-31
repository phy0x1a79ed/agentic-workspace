#!/usr/bin/env bash
# Dev harness for the web-ui.
#
#   ./run.sh              # start (default)
#   ./run.sh start        # start uvicorn + login bookmark server
#   ./run.sh stop         # stop both
#   ./run.sh restart      # stop + start
#   ./run.sh reset        # wipe sandbox state and re-seed (asks first)
#   ./run.sh seed         # (re)seed the sandbox DB without restarting
#   ./run.sh login        # print a one-shot login URL (one-line CLI form)
#   ./run.sh status       # what's running, what URLs
#   ./run.sh logs         # tail the uvicorn log
#
# State lives in this directory (.awm/, projects/, data/) and is gitignored.
# Backend Python reloads via uvicorn --reload; static HTML/JS reloads on
# browser refresh (no build step).
#
# Two processes:
#   - uvicorn on https://127.0.0.1:7821 (the actual app + /ui SPA)
#   - login_server.py on http://127.0.0.1:7822 (a single-page bookmark
#     that mints a fresh /auth/bootstrap URL on each refresh; you'd
#     bookmark this in your browser)
#
# Why HTTPS for uvicorn: awm.exposed sets cookies with secure=True, so a
# plain-HTTP browser would silently drop them. The cert/key are
# auto-generated under .awm/tls/. Why HTTP for the login bookmark: it
# only renders a single-use 60s-TTL nonce URL — no secrets — so plain
# HTTP is fine and avoids a second cert.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"

# Per-worktree env overrides (gitignored). Sourced before computing port
# defaults so .env can pin AWM_EXPOSED_PORT etc. without env exports.
if [ -f "$HERE/.env" ]; then
  # shellcheck disable=SC1091
  set -a; . "$HERE/.env"; set +a
fi

# Scope-aware default port band — each worktree (dev/web-ui/web-backend/…)
# gets a unique band so three sandboxes can run side-by-side. Override by
# setting AWM_EXPOSED_PORT / AWM_DEV_LOGIN_PORT / VITE_PORT explicitly (env
# or dev/.env).
case "$(basename "$(realpath "$REPO_ROOT")")" in
  dev)         _PORT_BASE=782; _VITE_PORT=12103 ;;
  web-ui)      _PORT_BASE=783; _VITE_PORT=12113 ;;
  web-backend) _PORT_BASE=784; _VITE_PORT=12123 ;;
  *)           _PORT_BASE=785; _VITE_PORT=12153 ;;
esac

PID_FILE="$HERE/.awm/dev.pid"
LOG_FILE="$HERE/.awm/dev.log"
LOGIN_PID_FILE="$HERE/.awm/login.pid"
LOGIN_LOG_FILE="$HERE/.awm/login.log"
CERT_FILE="$HERE/.awm/tls/cert.pem"
KEY_FILE="$HERE/.awm/tls/key.pem"

export AWM_WORKSPACE="$HERE"
export AWM_EXPOSED_PORT="${AWM_EXPOSED_PORT:-${_PORT_BASE}1}"
export AWM_EXPOSED_HOST="${AWM_EXPOSED_HOST:-127.0.0.1}"
export AWM_DEV_LOGIN_PORT="${AWM_DEV_LOGIN_PORT:-${_PORT_BASE}2}"
export AWM_DEV_LOGIN_HOST="${AWM_DEV_LOGIN_HOST:-127.0.0.1}"
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

URL="https://${AWM_EXPOSED_HOST}:${AWM_EXPOSED_PORT}/ui/"
LOGIN_URL="http://${AWM_DEV_LOGIN_HOST}:${AWM_DEV_LOGIN_PORT}/"

cmd="${1:-start}"

is_running_pidfile() {
  local pf="$1"
  [ -f "$pf" ] && kill -0 "$(cat "$pf")" 2>/dev/null
}
is_running()       { is_running_pidfile "$PID_FILE"; }
is_login_running() { is_running_pidfile "$LOGIN_PID_FILE"; }

prep() {
  mkdir -p "$HERE/.awm"
  (cd "$REPO_ROOT" && mamba run -n awm --no-capture-output \
      python "$HERE/_prep.py" "$@")
}

_port_listeners() {
  # PIDs listening on $1 (default $AWM_EXPOSED_PORT).
  local port="${1:-$AWM_EXPOSED_PORT}"
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

do_stop() {
  local s1 s2
  s1="$(_stop_one "$PID_FILE"       "$AWM_EXPOSED_PORT"   "uvicorn")"
  s2="$(_stop_one "$LOGIN_PID_FILE" "$AWM_DEV_LOGIN_PORT" "login-server")"
  if [ "$s1" = "0" ] && [ "$s2" = "0" ]; then
    echo "[dev] not running"
  fi
}

do_seed() {
  prep
  echo "[dev] seeding sandbox"
  (cd "$REPO_ROOT" && mamba run -n awm --no-capture-output \
      python "$HERE/seed.py")
}

do_login() {
  if ! is_running; then
    echo "[dev] not running — start the harness first"
    exit 1
  fi
  local token
  token="$(cat "$HERE/.awm/auth.token" 2>/dev/null || true)"
  if [ -z "$token" ]; then
    echo "[dev] no auth.token — run 'start' once to bootstrap"
    exit 1
  fi
  local user="${AWM_DEV_USER:-dev}"
  local resp
  resp="$(curl -sk -X POST "https://${AWM_EXPOSED_HOST}:${AWM_EXPOSED_PORT}/auth/mint" \
            -H "Authorization: Bearer $token" \
            -H "Content-Type: application/json" \
            -d "{\"awm_user\":\"$user\"}")"
  local url
  url="$(printf '%s' "$resp" \
    | mamba run -n awm --no-capture-output python -c \
        'import json,sys; print(json.load(sys.stdin).get("url",""))' 2>/dev/null)"
  if [ -z "$url" ]; then
    echo "[dev] /auth/mint returned no url; response: $resp"
    exit 1
  fi
  echo "[dev] one-shot login URL (60s TTL):"
  echo
  echo "      $url"
  echo
  echo "[dev] tip: bookmark $LOGIN_URL for a self-refreshing version"
}

_start_uvicorn() {
  echo "[dev] uvicorn  $URL"
  cd "$REPO_ROOT"
  nohup setsid mamba run -n awm --no-capture-output \
      uvicorn awm.exposed:app \
      --host "$AWM_EXPOSED_HOST" --port "$AWM_EXPOSED_PORT" \
      --ssl-certfile "$CERT_FILE" --ssl-keyfile "$KEY_FILE" \
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

_start_login() {
  echo "[dev] login    $LOGIN_URL  (bookmark me — fresh link each refresh)"
  cd "$REPO_ROOT"
  nohup setsid mamba run -n awm --no-capture-output \
      python "$HERE/login_server.py" \
      >"$LOGIN_LOG_FILE" 2>&1 &
  echo $! >"$LOGIN_PID_FILE"
  sleep 0.5
  if ! is_login_running; then
    echo "[dev] login-server failed to start — see $LOGIN_LOG_FILE"
    tail -n 20 "$LOGIN_LOG_FILE" || true
    rm -f "$LOGIN_PID_FILE"
    # Don't abort the harness — uvicorn alone is still useful.
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
  if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    echo "[dev] TLS cert/key missing after prep — aborting"
    exit 1
  fi
  _start_uvicorn
  _start_login
  echo "[dev] logs: $LOG_FILE"
}

do_status() {
  local any=0
  if is_running; then
    echo "[dev] uvicorn       running (pid $(cat "$PID_FILE"))"
    echo "[dev]   $URL"
    any=1
  fi
  if is_login_running; then
    echo "[dev] login-server  running (pid $(cat "$LOGIN_PID_FILE"))"
    echo "[dev]   $LOGIN_URL"
    any=1
  fi
  [ "$any" -eq 0 ] && echo "[dev] not running"
}

do_reset() {
  if is_running || is_login_running; then
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

FRONTEND_DIR="$REPO_ROOT/frontend"
NODE_BIN="/home/tony/lib/miniforge3/envs/awm/bin"

do_frontend() {
  if [ ! -d "$FRONTEND_DIR" ]; then
    echo "[dev] no frontend/ directory — nothing to run"
    exit 1
  fi
  if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
    echo "[dev] installing frontend deps…"
    (cd "$FRONTEND_DIR" && PATH="$NODE_BIN:$PATH" npm install --no-audit --no-fund)
  fi
  # AWM_API_TARGET defaults to this worktree's own uvicorn; override to point
  # Vite at a different scope's backend (e.g. web-backend's uvicorn on 7841).
  local api_target="${AWM_API_TARGET:-https://${AWM_EXPOSED_HOST}:${AWM_EXPOSED_PORT}}"
  echo "[dev] vite dev → http://0.0.0.0:${VITE_PORT}/ui/  (proxies API/WS to ${api_target})"
  (cd "$FRONTEND_DIR" && PATH="$NODE_BIN:$PATH" \
      AWM_API_TARGET="$api_target" VITE_PORT="$VITE_PORT" \
      npm run dev -- --host 0.0.0.0 --port "$VITE_PORT")
}

do_build() {
  if [ ! -d "$FRONTEND_DIR" ]; then
    echo "[dev] no frontend/ directory — nothing to build"
    exit 1
  fi
  # adapter-static empties awm/static/ before writing the SPA, which wipes
  # the server-rendered login.html (operator lockout risk). Preserve it
  # across the build by copy-aside-then-restore.
  local login_src="$REPO_ROOT/awm/static/login.html"
  local login_bak=""
  if [ -f "$login_src" ]; then
    login_bak="$(mktemp)"
    cp "$login_src" "$login_bak"
  fi
  (cd "$FRONTEND_DIR" && PATH="$NODE_BIN:$PATH" npm install --no-audit --no-fund && PATH="$NODE_BIN:$PATH" npm run build)
  if [ -n "$login_bak" ]; then
    cp "$login_bak" "$login_src"
    rm -f "$login_bak"
    echo "[dev] preserved login.html across build"
  fi
  echo "[dev] build output → $REPO_ROOT/awm/static/  (restart uvicorn to pick up)"
}

case "$cmd" in
  start)    do_start ;;
  stop)     do_stop ;;
  restart)  do_stop; do_start ;;
  status)   do_status ;;
  seed)     do_seed ;;
  reset)    do_reset ;;
  login)    do_login ;;
  logs)     tail -n 200 -F "$LOG_FILE" ;;
  frontend) do_frontend ;;
  build)    do_build ;;
  *)
    echo "usage: $0 {start|stop|restart|status|seed|reset|login|logs|frontend|build}"
    exit 2
    ;;
esac

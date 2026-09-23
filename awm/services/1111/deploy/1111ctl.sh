#!/bin/bash
# Lifecycle control for the stable-diffusion-webui process, run as u1111.
# Usage: 1111ctl.sh start|stop|status|restart
#
# Modeled on awm's trilium service (awm/services/trilium/awm/trilium/server.py):
# own session group so the whole tree can be signalled, SIGTERM then SIGKILL
# on stop, a PID file (not just a port probe) so "is it running" survives a
# webui that's still binding its port.
set -u

APP_DIR="$HOME/stable-diffusion-webui"
RUN_DIR="$HOME/run"
PID_FILE="$RUN_DIR/webui.pid"
LOG_FILE="$RUN_DIR/webui.log"
PORT=17860
HOST=127.0.0.1
# Must match the prefix awm/svc1111/register.py registers on the gateway.
SUBPATH=1111

mkdir -p "$RUN_DIR"

_pid_alive() {
    [[ -f "$PID_FILE" ]] || return 1
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null)"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

_port_listening() {
    (exec 3<>"/dev/tcp/$HOST/$PORT") 2>/dev/null && exec 3<&- 3>&-
}

cmd_status() {
    if _pid_alive; then
        echo "running pid=$(cat "$PID_FILE") port=$PORT listening=$(_port_listening && echo yes || echo no)"
        exit 0
    fi
    echo "stopped"
    exit 1
}

cmd_start() {
    if _pid_alive; then
        echo "already-running pid=$(cat "$PID_FILE")"
        exit 0
    fi
    cd "$APP_DIR" || { echo "no app dir at $APP_DIR"; exit 1; }
    # setsid: own process group, so stop() can signal the whole tree at once.
    # `bash ./webui.sh` (not `exec ./webui.sh`): the checkout came off a
    # Windows drive and webui.sh has CRLF line endings, which breaks its own
    # `#!/usr/bin/env bash` shebang. Invoking bash directly on the file
    # sidesteps shebang parsing entirely.
    # PIP_CONSTRAINT / PIP_BUILD_CONSTRAINT: pip's isolated build env for old
    # setup.py-only packages (openai/CLIP) fetches the newest setuptools,
    # which dropped bundled pkg_resources — pin it older so those legacy
    # builds still work. Build-time deps need the separate --build-constraint
    # flag (PIP_CONSTRAINT alone does not reach the isolated build env).
    # --subpath: webui's own reverse-proxy support (passed through to
    # Gradio's root_path) — without it Gradio emits root-relative asset URLs
    # that bypass the gateway's /1111 prefix entirely and 404 in the browser.
    setsid bash -c "
        export PIP_CONSTRAINT='$RUN_DIR/pip-constraints.txt'
        export PIP_BUILD_CONSTRAINT='$RUN_DIR/pip-constraints.txt'
        exec bash ./webui.sh --listen --port $PORT --subpath $SUBPATH >> '$LOG_FILE' 2>&1
    " &
    child=$!
    echo "$child" > "$PID_FILE"
    disown "$child" 2>/dev/null
    echo "started pid=$child"
}

cmd_stop() {
    if ! _pid_alive; then
        rm -f "$PID_FILE"
        echo "not-running"
        exit 0
    fi
    local pid pgid
    pid="$(cat "$PID_FILE")"
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')"
    if [[ -n "$pgid" ]]; then
        kill -TERM "-$pgid" 2>/dev/null
    else
        kill -TERM "$pid" 2>/dev/null
    fi
    for _ in $(seq 1 20); do
        _pid_alive || break
        sleep 1
    done
    if _pid_alive; then
        [[ -n "$pgid" ]] && kill -KILL "-$pgid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null
    fi
    rm -f "$PID_FILE"
    echo "stopped"
}

cmd_restart() {
    cmd_stop
    cmd_start
}

case "${1:-}" in
    start) cmd_start ;;
    stop) cmd_stop ;;
    status) cmd_status ;;
    restart) cmd_restart ;;
    *) echo "usage: $0 start|stop|status|restart" >&2; exit 2 ;;
esac

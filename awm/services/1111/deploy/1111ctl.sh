#!/bin/bash
# Lifecycle control for the webui and the gallery viewer, run as u1111.
# Usage: 1111ctl.sh start|stop|status|restart|view-start|view-stop|view-status|view-restart
#
# Modeled on awm's trilium service (awm/services/trilium/awm/trilium/server.py):
# own session group so the whole tree can be signalled, SIGTERM then SIGKILL
# on stop, a PID file (not just a port probe) so "is it running" survives a
# process that's still binding its port.
set -u

APP_DIR="$HOME/stable-diffusion-webui"
VIEW_DIR="$HOME/view"
RUN_DIR="$HOME/run"
HOST=127.0.0.1
# Ports and subpath must match awm/svc1111/control.py and register.py.
WEBUI_PORT=17860
VIEW_PORT=17861
SUBPATH=1111

mkdir -p "$RUN_DIR"

_pid_file() { echo "$RUN_DIR/$1.pid"; }

_pid_alive() {
    local f pid
    f="$(_pid_file "$1")"
    [[ -f "$f" ]] || return 1
    pid="$(cat "$f" 2>/dev/null)"
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

_port_listening() {
    (exec 3<>"/dev/tcp/$HOST/$1") 2>/dev/null && exec 3<&- 3>&-
}

_status() {
    local name=$1 port=$2
    if _pid_alive "$name"; then
        echo "running pid=$(cat "$(_pid_file "$name")") port=$port listening=$(_port_listening "$port" && echo yes || echo no)"
        exit 0
    fi
    echo "stopped"
    exit 1
}

# _launch NAME DIR COMMAND: run COMMAND from DIR in its own session group,
# appending to NAME.log, and record the group leader in NAME.pid.
_launch() {
    local name=$1 dir=$2 command=$3
    if _pid_alive "$name"; then
        echo "already-running pid=$(cat "$(_pid_file "$name")")"
        exit 0
    fi
    cd "$dir" || { echo "no dir at $dir"; exit 1; }
    setsid bash -c "$command >> '$RUN_DIR/$name.log' 2>&1" &
    local child=$!
    echo "$child" > "$(_pid_file "$name")"
    disown "$child" 2>/dev/null
    echo "started pid=$child"
}

_stop() {
    local name=$1 f pid pgid
    f="$(_pid_file "$name")"
    if ! _pid_alive "$name"; then
        rm -f "$f"
        echo "not-running"
        return 0
    fi
    pid="$(cat "$f")"
    pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')"
    if [[ -n "$pgid" ]]; then
        kill -TERM "-$pgid" 2>/dev/null
    else
        kill -TERM "$pid" 2>/dev/null
    fi
    for _ in $(seq 1 20); do
        _pid_alive "$name" || break
        sleep 1
    done
    if _pid_alive "$name"; then
        [[ -n "$pgid" ]] && kill -KILL "-$pgid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null
    fi
    rm -f "$f"
    echo "stopped"
}

# `bash ./webui.sh` (not `exec ./webui.sh`): the checkout came off a Windows
# drive and webui.sh has CRLF line endings, which breaks its own
# `#!/usr/bin/env bash` shebang. Invoking bash directly on the file sidesteps
# shebang parsing entirely.
# PIP_CONSTRAINT / PIP_BUILD_CONSTRAINT: pip's isolated build env for old
# setup.py-only packages (openai/CLIP) fetches the newest setuptools, which
# dropped bundled pkg_resources — pin it older so those legacy builds still
# work. Build-time deps need the separate --build-constraint flag
# (PIP_CONSTRAINT alone does not reach the isolated build env).
# --subpath: webui's own reverse-proxy support (passed through to Gradio's
# root_path) — without it Gradio emits root-relative asset URLs that bypass
# the gateway's /1111 prefix entirely and 404 in the browser.
webui_start() {
    _launch webui "$APP_DIR" "
        export PIP_CONSTRAINT='$RUN_DIR/pip-constraints.txt'
        export PIP_BUILD_CONSTRAINT='$RUN_DIR/pip-constraints.txt'
        exec bash ./webui.sh --listen --port $WEBUI_PORT --subpath $SUBPATH"
}

# The viewer borrows the webui's venv for FastAPI, uvicorn and Pillow.
view_start() {
    _launch view "$VIEW_DIR" \
        "exec '$APP_DIR/venv/bin/python' -m uvicorn server:app --host $HOST --port $VIEW_PORT"
}

case "${1:-}" in
    start) webui_start ;;
    stop) _stop webui ;;
    status) _status webui "$WEBUI_PORT" ;;
    restart) _stop webui; webui_start ;;
    view-start) view_start ;;
    view-stop) _stop view ;;
    view-status) _status view "$VIEW_PORT" ;;
    view-restart) _stop view; view_start ;;
    *) echo "usage: $0 start|stop|status|restart|view-start|view-stop|view-status|view-restart" >&2; exit 2 ;;
esac

#!/bin/bash
# rlm-browser container entrypoint.
#
# Builds the active supervisord config from the common base + the mode-specific
# program blocks, then execs supervisord as PID 1. Two modes:
#
#   simple   : headless Chrome only.
#   rendered : Xvfb -> headful Chrome -> x11vnc -> noVNC (watchable in a browser
#              at http://127.0.0.1:$RLM_NOVNC_PORT/vnc.html).
#
# Both expose CDP on 0.0.0.0:$RLM_CDP_PORT inside the container; the host maps it
# 1:1 to 127.0.0.1:$RLM_CDP_PORT so Chrome's advertised webSocketDebuggerUrl is
# dialable as-is.
set -euo pipefail

MODE="${RLM_MODE:-simple}"
CDP_PORT="${RLM_CDP_PORT:-9222}"
NOVNC_PORT="${RLM_NOVNC_PORT:-7900}"
# Chrome's DevTools HTTP server ALWAYS binds loopback inside the container —
# headless Chrome ignores --remote-debugging-address=0.0.0.0 (a long-standing
# upstream behaviour). So Chrome listens on 127.0.0.1:$CHROME_PORT and a socat
# bridge republishes it on 0.0.0.0:$CDP_PORT, which is what the host maps 1:1.
# $CHROME_PORT is deliberately distinct from any host-allocated $CDP_PORT (the
# docker backend hands out ephemeral ports; this default never collides).
CHROME_PORT=9223
CONF=/tmp/rlm.conf

# Chrome flags shared by both modes. --no-sandbox is required (running as a
# non-root user in an unprivileged container); --disable-dev-shm-usage is a
# belt-and-braces partner to the host's --shm-size=2g.
CHROME_COMMON="\
--remote-debugging-port=${CHROME_PORT} \
--remote-debugging-address=127.0.0.1 \
--remote-allow-origins=* \
--user-data-dir=/data \
--no-sandbox \
--disable-dev-shm-usage \
--no-first-run \
--no-default-browser-check \
--disable-background-networking \
--disable-extensions"

cp /etc/supervisor/rlm.conf "$CONF"

# The CDP bridge: expose Chrome's loopback DevTools port on all interfaces so the
# host's published port reaches it. Shared by both modes.
cat >> "$CONF" <<EOF

[program:cdp_bridge]
priority=50
command=socat TCP-LISTEN:${CDP_PORT},fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:${CHROME_PORT}
autorestart=true
startretries=999
stdout_logfile=/var/log/rlm/socat.log
redirect_stderr=true
EOF

if [ "$MODE" = "rendered" ]; then
  cat >> "$CONF" <<EOF

[program:xvfb]
priority=10
command=Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp
autorestart=true
stdout_logfile=/var/log/rlm/xvfb.log
redirect_stderr=true

[program:chrome]
priority=20
command=google-chrome-stable ${CHROME_COMMON} about:blank
environment=DISPLAY=":99"
autorestart=true
startretries=3
startsecs=3
stdout_logfile=/var/log/rlm/chrome.log
redirect_stderr=true

[program:x11vnc]
priority=30
command=x11vnc -display :99 -forever -shared -nopw -rfbport 5900 -quiet
autorestart=true
stdout_logfile=/var/log/rlm/x11vnc.log
redirect_stderr=true

[program:novnc]
priority=40
command=websockify --web /usr/share/novnc ${NOVNC_PORT} localhost:5900
autorestart=true
stdout_logfile=/var/log/rlm/novnc.log
redirect_stderr=true
EOF
else
  cat >> "$CONF" <<EOF

[program:chrome]
priority=20
command=google-chrome-stable ${CHROME_COMMON} --headless=new about:blank
autorestart=true
startretries=3
startsecs=3
stdout_logfile=/var/log/rlm/chrome.log
redirect_stderr=true
EOF
fi

exec supervisord -c "$CONF" -n

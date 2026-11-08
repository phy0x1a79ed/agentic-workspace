# rlm-browser Chrome image — ONE image, two modes via $RLM_MODE.
#
#   simple   : headless Chrome only (lightweight; the "give me a DOM" path).
#   rendered : Xvfb + headful Chrome + x11vnc + noVNC (human-watchable).
#
# Both present CDP on $RLM_CDP_PORT bound to 0.0.0.0 *inside* the container; the
# host maps 127.0.0.1:<port>:<port> 1:1 so the webSocketDebuggerUrl Chrome
# advertises is dialable from the host as-is (Chrome >=111 validates the Host
# header is an IP — we always connect by 127.0.0.1, never a hostname).
#
# PID 1 is supervisord (mirrors projects/vpn_bounce) so T4 can wrap the whole
# container in a VPN netns with a quit_on_failure kill-switch — if Chrome dies,
# supervisord brings the container down rather than leaking traffic.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# Chrome + a virtual X stack + VNC bridge + supervisor.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg wget \
        xvfb x11vnc \
        novnc websockify \
        socat \
        supervisor \
        fonts-liberation libnss3 libatk-bridge2.0-0 libgtk-3-0 libgbm1 \
        libasound2 libxshmfence1 libxdamage1 libxcomposite1 libxrandr2 \
    && install -d -m 0755 /etc/apt/keyrings \
    && wget -qO- https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# Chrome refuses --no-sandbox as root, so run as a dedicated non-root user. The
# profile lives at /data (a bind mount on the host's ext4, never /mnt/c).
RUN useradd -m -u 1000 -s /bin/bash chrome \
    && install -d -o chrome -g chrome /data /var/log/rlm
USER chrome

ENV RLM_MODE=simple \
    RLM_CDP_PORT=9222 \
    RLM_NOVNC_PORT=7900 \
    DISPLAY=:99 \
    HOME=/home/chrome

COPY supervisord.conf /etc/supervisor/rlm.conf
COPY entrypoint.sh    /usr/local/bin/entrypoint.sh

EXPOSE 9222 7900
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]

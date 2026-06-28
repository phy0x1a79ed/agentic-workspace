# Proton VPN exit — protonvpn-cli behind the same fail-closed kill-switch.
#
# Consumers attach to this container's netns (`--network=container:awm-vpn-proton`).
# protonvpn-cli is community/OpenVPN-backed; the exact login/connect/status
# invocation is version-sensitive (see INSTALL.md § Proton) — dial.sh is written
# to the common shape and is the one place to adjust if your CLI build differs.
#
# Build (from this directory):
#     docker build -t awm-vpn-proton .
# The awm `vpn` service injects creds as env and adds
# --cap-add=NET_ADMIN --device=/dev/net/tun at run time.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        openvpn \
        curl \
        ca-certificates \
        iproute2 \
        procps \
        python3 \
        python3-pip \
        dialog \
        supervisor \
        openssh-server \
    && rm -rf /var/lib/apt/lists/*

# Community protonvpn-cli. (--break-system-packages for PEP 668 on bookworm.)
RUN pip3 install --no-cache-dir --break-system-packages protonvpn-cli

COPY supervisord.conf /etc/supervisord.conf
COPY sshd_config      /etc/ssh/sshd_config
COPY dial.sh          /usr/local/bin/dial.sh
COPY ssh_server       /usr/local/bin/ssh_server
RUN chmod +x /usr/local/bin/dial.sh /usr/local/bin/ssh_server

EXPOSE 22
ENTRYPOINT ["supervisord", "-c", "/etc/supervisord.conf"]

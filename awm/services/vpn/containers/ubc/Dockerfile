# UBC VPN exit — openconnect to myvpn.ubc.ca behind a fail-closed kill-switch,
# with a co-located bounce sshd (the proven projects/vpn_bounce architecture).
#
# Two ways to route through this exit:
#   - netns attach: `--network=container:awm-vpn-ubc` (browsers, docker'd procs).
#   - SSH ProxyJump: the bounce sshd shares this container's netns, so an
#     `ssh -W %h:%p` through localhost:<published-port> egresses via the tunnel.
#     This is how host-side `ssh sockeye` reaches UBC ARC (see ~/.ssh/config).
# Either way the kill-switch holds: if openconnect exits, the whole container
# (and every consumer's egress) goes down with it.
#
# Build (from this directory):
#     docker build -t awm-vpn-ubc .
# Run is done by the awm `vpn` service (container.py): it injects creds + the
# bounce public key as env, publishes the bounce port, and adds
# --cap-add=NET_ADMIN --device=/dev/net/tun.
FROM alpine:edge

RUN apk add --update --no-cache \
    curl \
    openconnect \
    iproute2 \
    supervisor \
    openssh

COPY supervisord.conf /etc/supervisord.conf
COPY openssl.conf     /etc/openssl.conf
COPY sshd_config      /etc/ssh/sshd_config
COPY dial.sh          /usr/local/bin/dial.sh
COPY ssh_server       /usr/local/bin/ssh_server
RUN chmod +x /usr/local/bin/dial.sh /usr/local/bin/ssh_server

# UnsafeLegacyRenegotiation: UBC's Cisco ASA endpoint needs legacy TLS reneg.
ENV OPENSSL_CONF=/etc/openssl.conf

EXPOSE 22
ENTRYPOINT ["supervisord", "-c", "/etc/supervisord.conf"]

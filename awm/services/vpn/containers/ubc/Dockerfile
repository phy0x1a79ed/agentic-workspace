# UBC VPN exit — openconnect to myvpn.ubc.ca behind a fail-closed kill-switch.
#
# Ported from projects/vpn_bounce (the proven UBC openconnect pattern), minus the
# sshd/bounce machinery: svc-vpn consumers attach to this container's network
# namespace (`--network=container:awm-vpn-ubc`), they don't ssh in. The only job
# here is "hold a UBC tunnel up, and if it ever drops, take the whole container
# (hence every consumer's egress) down with it."
#
# Build (from this directory):
#     docker build -t awm-vpn-ubc .
# Run is done by the awm `vpn` service (container.py): it injects creds as env and
# adds --cap-add=NET_ADMIN --device=/dev/net/tun.
FROM alpine:edge

RUN apk add --update --no-cache \
    curl \
    openconnect \
    iproute2 \
    supervisor

COPY supervisord.conf /etc/supervisord.conf
COPY openssl.conf     /etc/openssl.conf
COPY dial.sh          /usr/local/bin/dial.sh
RUN chmod +x /usr/local/bin/dial.sh

# UnsafeLegacyRenegotiation: UBC's Cisco ASA endpoint needs legacy TLS reneg.
ENV OPENSSL_CONF=/etc/openssl.conf

ENTRYPOINT ["supervisord", "-c", "/etc/supervisord.conf"]

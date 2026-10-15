#!/usr/bin/env bash
# Locks down the DOCKER-USER chain so Docker's own DNAT/FORWARD rules can
# never make a published container port reachable from off-host, even if a
# container is started with a bare `-p <port>:<port>` (binds 0.0.0.0).
#
# Docker guarantees DOCKER-USER is consulted before its own published-port
# rules, and this chain survives `systemctl restart docker` (dockerd only
# ensures the chain exists; it does not flush admin-added rules). It does
# NOT survive a host reboot (netfilter tables are empty after boot), which
# is why this script is also wired into a systemd unit that reapplies it
# after docker.service starts.
#
# Idempotent: flush-then-rebuild, safe to run repeatedly.
set -euo pipefail

CHAIN="DOCKER-USER"
# Non-loopback, non-docker interfaces this host has. Traffic arriving on
# any of these is "off-host" and must never reach a container's published
# port directly -- only nginx (on loopback) is meant to front anything.
EXT_IFACES="eth0 eth1"

iptables -N "$CHAIN" 2>/dev/null || true
iptables -F "$CHAIN"

# Let return traffic for connections the host/containers themselves opened
# (e.g. a container pulling something from the internet) back in.
iptables -A "$CHAIN" -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN

# Loopback and the docker bridge are "local" -- host-to-container and
# container-to-container traffic is fine.
iptables -A "$CHAIN" -i lo -j RETURN
iptables -A "$CHAIN" -i docker0 -j RETURN

# Anything arriving from an external interface is off-host: never let it
# reach a docker-published port.
for ifc in $EXT_IFACES; do
  iptables -A "$CHAIN" -i "$ifc" -j DROP 2>/dev/null || true
done

# Fall through to normal FORWARD/DOCKER processing for anything else
# (e.g. a future docker network on some other bridge).
iptables -A "$CHAIN" -j RETURN

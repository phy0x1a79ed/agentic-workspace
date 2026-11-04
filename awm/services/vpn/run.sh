#!/usr/bin/env bash
# run.sh — self-contained entry point for the vpn egress service.
#
# The gateway discovers this service by scanning awm/services/* for run.sh, and
# starts (and respawns) it by executing `bash run.sh`, injecting AWM_HUB_URL /
# AWM_SERVICE_NAME / AWM_SERVICE_ID into the environment. The adapter reads
# those, POSTs /hub/service/register, and holds the control WS open. No auth.
#
# The service only ORCHESTRATES docker — every privileged VPN primitive
# (NET_ADMIN, /dev/net/tun, routing, the openconnect/protonvpn dialers, and the
# fail-closed kill-switch) lives INSIDE the per-profile container. So this is a
# plain Python feature service; it needs nothing privileged itself beyond being
# able to talk to the docker daemon.
set -euo pipefail
cd "$(dirname "$0")"
MODULE="awm.vpn.hub_adapter"

[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
exec "${AWM_PYTHON:-python}" -m "$MODULE"

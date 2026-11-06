#!/usr/bin/env bash
# This service owns two things the gateway registration does not carry:
#   - the DeepSeek Harness web server, a node process parented to this one and
#     bound to loopback (see awm.dsh.harness),
#   - one HTTPS front on the mesh, reusing awm.tlsfront wholesale so the GUI
#     sits behind awm's edge session (see awm.dsh.front).
# Both die with this process, which is right: the harness persists its sessions
# to its own store, so a deploy costs a reconnect rather than a conversation.
set -euo pipefail
cd "$(dirname "$0")"
MODULE="awm.dsh.hub_adapter"

[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
exec "${AWM_PYTHON:-python}" -m "$MODULE"

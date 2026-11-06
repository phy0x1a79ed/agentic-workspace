#!/usr/bin/env bash
# This service owns three things the gateway registration does not carry:
#   - the Claude Science workbench daemon, a foreign binary that is *adopted*
#     rather than parented (see awm.claude_science.daemon for why),
#   - two HTTPS fronts on the mesh, one per workbench origin, reusing
#     awm.tlsfront wholesale so both sit behind awm's edge session, and
#   - an MCP bridge that hands the workbench a curated slice of awm's verbs.
# The fronts and the bridge die with this process; the daemon deliberately does
# not, so an awm deploy never interrupts a conversation.
set -euo pipefail
cd "$(dirname "$0")"
MODULE="awm.claude_science.hub_adapter"

[ -f ./.runtime-env ] && . ./.runtime-env
[ -n "${AWM_ENV_BIN:-}" ] && export PATH="$AWM_ENV_BIN:$PATH"
exec "${AWM_PYTHON:-python}" -m "$MODULE"

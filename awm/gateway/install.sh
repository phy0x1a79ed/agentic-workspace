#!/usr/bin/env bash
# Canonical install for awm-gateway — the composition root.
#
# The gateway dynamically loads every feature module, so installing it means
# installing the whole modular tree: the component libraries, every feature
# service, then the gateway itself (which provides the `awm` / `awm-mcp`
# console scripts). This is also the de-facto "install everything" path; there
# is no separate central orchestrator. Override the target env with AWM_ENV.
#
# Service installation is single-source: there is no hardcoded list of service
# names here. Every dir under awm/services/ that ships its own install.sh is
# discovered by glob and installed by running that script — each one owns its
# own `.runtime-env` sidecar. Dirs without an install.sh (tts, stt, network,
# packages, hub) are correctly skipped.
set -euo pipefail
WS="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
export AWM_ENV="${AWM_ENV:-awm}"
ENV="$AWM_ENV"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

# Component libraries (imported, not run on their own). The per-service
# installs re-install these idempotently; installing them here keeps the
# gateway self-sufficient if no services ship an install.sh.
run "$WS/awm/service_components/config" --no-deps
run "$WS/awm/service_components/persistence" --no-deps
run "$WS/awm/service_components/gatewayclient" --no-deps

# Feature services the gateway loads. Each install.sh owns its own
# `.runtime-env` sidecar. The glob may match nothing; guard so set -u/-e does
# not choke on the literal unexpanded pattern.
# AWM_SERVICES, when set, is a space-separated allow-list of service names:
# a host that enables only a few services installs only those.
for s in "$WS"/awm/services/*/install.sh; do
    [ -e "$s" ] || continue
    name="$(basename "$(dirname "$s")")"
    if [ -n "${AWM_SERVICES:-}" ] && ! grep -qw -- "$name" <<<"$AWM_SERVICES"; then
        echo "- skipping service (not in AWM_SERVICES): $name"
        continue
    fi
    echo "+ installing service: $s"
    bash "$s"
done

# The gateway itself — third-party deps resolved here; awm-* already satisfied.
run "$WS/awm/gateway"

echo "Installed awm-gateway + all modules into env '$ENV'."

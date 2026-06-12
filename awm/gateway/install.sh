#!/usr/bin/env bash
# Canonical install for awm-gateway — the composition root.
#
# The gateway dynamically loads every feature module, so installing it means
# installing the whole modular tree: the component libraries, every feature
# service, then the gateway itself (which provides the `awm` / `awm-mcp`
# console scripts). This is also the de-facto "install everything" path; there
# is no separate central orchestrator. Override the target env with AWM_ENV.
set -euo pipefail
WS="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

# Component libraries (imported, not run on their own).
run "$WS/awm/service_components/config" --no-deps
run "$WS/awm/service_components/persistence" --no-deps

# Feature services the gateway loads.
for svc in scopes agents artifacts skills discord; do
    run "$WS/awm/services/$svc" --no-deps
done

# The gateway itself — third-party deps resolved here; awm-* already satisfied.
run "$WS/awm/gateway"

echo "Installed awm-gateway + all modules into env '$ENV'."

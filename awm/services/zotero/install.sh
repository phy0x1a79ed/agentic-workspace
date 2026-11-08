#!/usr/bin/env bash
# Build the zotero plugin's own runtime: the conda env awm-zotero, or AWM_ENV.
#
# This service owns no database: its state is the bundle in the vault scope, so
# it does not depend on awm-persistence. The library is read with `curl` over
# `ssh` on the node the Zotero desktop is on, and the vault is reached over the
# trilium service's verbs: no client library, no credential, no daemon.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWM="${AWM_ROOT:-$(git -C "$HERE" rev-parse --show-toplevel)/awm}"
ENV="${AWM_ENV:-awm-zotero}"

. "$AWM/plugin-env.sh"
run "$AWM/service_components/config" --no-deps
run "$AWM/service_components/gatewayclient" --no-deps
run "$AWM/service_components/grantlib"
run "$HERE"

bake_runtime_env

echo "Installed awm-zotero into env '$ENV'."

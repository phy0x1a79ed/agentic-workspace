#!/usr/bin/env bash
# Entry point for the tts service. The hub injects AWM_HUB_URL,
# AWM_HUB_TOKEN, AWM_SERVICE_NAME, AWM_SERVICE_ID in env; the adapter
# uses them to POST /hub/service/register and then hold a control WS.
#
# We cd into backend/ so sys.path[0] = .../backend/, which makes both
# `import voice` (the sibling engine registry) and the in-package
# absolute imports (`from app import ...`, `import presets`) resolve
# without any package qualifier.
#
# PYTHONPATH points at the worktree root so `import awm` resolves to
# this dev tree's awm/ — without it, the conda editable install maps
# `awm` to the release worktree (see memory awm_two_source_trees).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# packages/services/tts/start.sh → REPO_ROOT is 4 levels up.
REPO_ROOT="$(cd "$HERE/../../.." && pwd)"
cd "$HERE/backend"
exec mamba run -n awm --no-capture-output \
  env PYTHONPATH="$REPO_ROOT" python hub_adapter.py

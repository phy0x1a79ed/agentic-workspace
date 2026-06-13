#!/usr/bin/env bash
# Saved dev-shadow templates. Run from the scope worktree root.
#
# Each case pushes one or more packages as shadow overlays onto the
# running dev sandbox's hub (https://127.0.0.1:7821/). The base
# packages stay registered; traffic redirects to the shadow while
# this script is running and falls back instantly on Ctrl-C.
#
# Add a new case here when a new common workflow emerges; merge back
# to dev so all scopes inherit it.
set -euo pipefail

# Shadows always target the running dev sandbox's hub, which lives on
# :7821 (the `dev` worktree's port band — see dev/run.sh). `awm dev
# shadow` derives its hub origin from AWM_PORT, which defaults to :7819
# (prod) when unset — so without this, running this script from any
# non-dev worktree silently aims shadows at prod and 409s with
# "no base registered". Pin to the dev hub unless the caller overrode it.
export AWM_PORT="${AWM_PORT:-7821}"

case "${1:-help}" in
  agent-stack) awm dev shadow services/ptt services/tts pages/agent ;;
  tts-only)    awm dev shadow services/tts pages/tts ;;
  ptt-only)    awm dev shadow services/ptt pages/ptt ;;
  voice)       awm dev shadow services/ptt services/tts pages/ptt pages/tts ;;
  help|*)      echo "templates: agent-stack | tts-only | ptt-only | voice"; exit 1 ;;
esac

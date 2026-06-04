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

case "${1:-help}" in
  agent-stack) awm dev shadow services/ptt services/tts pages/test-agent ;;
  tts-only)    awm dev shadow services/tts pages/tts ;;
  ptt-only)    awm dev shadow services/ptt pages/ptt ;;
  voice)       awm dev shadow services/ptt services/tts pages/ptt pages/tts ;;
  help|*)      echo "templates: agent-stack | tts-only | ptt-only | voice"; exit 1 ;;
esac

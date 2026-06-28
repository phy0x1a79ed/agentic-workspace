#!/usr/bin/env bash
#
# apply-gamebot-conventions.sh — STAGED cross-scope convention change for the gamebot effort.
#
# This script is PREPARED, not yet applied. It makes two edits to SHARED docs:
#   1. WORKSPACE.md  — adds the `rlm-*` realm-service prefix row to the scope-naming table.
#   2. WEB_GAMES_ON_AWM.md — appends a dated "deduped scope set" section that supersedes
#      its original Scope-roadmap + Open-questions (agent-runner -> agents service,
#      svc-timer -> svc-events, svc-realm-browser -> rlm-browser; composition-first kept).
#
# >>> RUN ONLY WHEN OTHER AGENTS ARE PAUSED. <<<
# Both targets are shared/active files:
#   - WORKSPACE.md is read by EVERY scope agent from the RELEASE worktree
#     ($WS_ROOT/WORKSPACE.md). Editing it dirties that worktree; once paused, commit it
#     on release (or merge feat -> dev -> release) so all scopes pick up the convention.
#   - WEB_GAMES_ON_AWM.md lives in the game-bot project — commit it there separately.
#
# Re-running is SAFE: each edit is grep-guarded to a no-op if already applied.
#
# Usage:
#   ./apply-gamebot-conventions.sh --dry-run     # report what would change, modify nothing
#   ./apply-gamebot-conventions.sh               # apply the edits in place
#   AWM_WORKSPACE_ROOT=/path ./apply-gamebot-conventions.sh   # override workspace root
#
set -euo pipefail

WS_ROOT="${AWM_WORKSPACE_ROOT:-/home/tony/agentic_workspace}"
WORKSPACE_MD="$WS_ROOT/WORKSPACE.md"
SPEC_MD="$WS_ROOT/projects/game-bot/web/WEB_GAMES_ON_AWM.md"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

note() { printf '[conventions] %s\n' "$*"; }

# --- 1. WORKSPACE.md: insert the rlm-* row right after the svc-* row -------------
add_rlm_prefix_row() {
  [[ -f "$WORKSPACE_MD" ]] || { note "SKIP: $WORKSPACE_MD not found"; return; }
  if grep -qE '^\| `rlm-\*`' "$WORKSPACE_MD"; then
    note "WORKSPACE.md: rlm-* row already present — no-op"; return
  fi
  if ! grep -qE '^\| `svc-\*`' "$WORKSPACE_MD"; then
    note "WARN: svc-* anchor row not found in $WORKSPACE_MD — not editing"; return
  fi
  local row='| `rlm-*`  | realm     | A substrate **realm** service wrapping one execution substrate (browser/android/direct) behind the realm-family contract. Lives in `awm/services/rlm-<type>/`. |'
  if [[ $DRY_RUN == 1 ]]; then
    note "DRY-RUN: would insert the rlm-* row after the svc-* row in WORKSPACE.md"; return
  fi
  awk -v row="$row" '
    { print }
    /^\| `svc-\*`/ && !done { print row; done=1 }
  ' "$WORKSPACE_MD" > "$WORKSPACE_MD.tmp" && mv "$WORKSPACE_MD.tmp" "$WORKSPACE_MD"
  note "WORKSPACE.md: inserted rlm-* row"
}

# --- 2. WEB_GAMES_ON_AWM.md: append the deduped-scope-set update -----------------
SPEC_MARKER='## Update (2026-06-27) — deduped scope set'
append_spec_update() {
  [[ -f "$SPEC_MD" ]] || { note "SKIP: $SPEC_MD not found"; return; }
  if grep -qF "$SPEC_MARKER" "$SPEC_MD"; then
    note "WEB_GAMES_ON_AWM.md: dedup update already present — no-op"; return
  fi
  if [[ $DRY_RUN == 1 ]]; then
    note "DRY-RUN: would append the deduped-scope-set update to WEB_GAMES_ON_AWM.md"; return
  fi
  cat >> "$SPEC_MD" <<'EOF'

## Update (2026-06-27) — deduped scope set

Supersedes the "Scope roadmap" + "Open questions" above, after deduping with Tony against awm's existing services and naming:

- **Composition-first kept.** `feat-gamebot` stays the long-lived composition + sandbox home (NOT retired); the service scopes are split out from day one.
- **Realm services use the new `rlm-*` prefix.** `svc-realm-browser` → **`rlm-browser`** (future realms → `rlm-android`, `rlm-direct`).
- **`svc-agent-runner` is dropped** — the agent-runner role IS the existing `agents` service (`awm/services/agents/`); reuse/extend rather than build a new service.
- **`svc-timer` → `svc-events`** — generalized to an agent event/notification service: timer (cron → `schedule.tick`) first, plus the funnel that normalizes realm/effector hooks (`game.*.needs_*`, `rlm.*.error`) into agent-wake notifications the `agents` service consumes.
- **All web realm/effector/agent/timer code lives under `awm/services/*`;** `projects/game-bot` is retired for web work (the factorio slice stays a standalone POC).

Resulting scopes (created + seeded 2026-06-27): `feat-gamebot` (composition), `rlm-browser`, `svc-effector`, `svc-events`. The gamebot sandbox runs on a pinned `:7871` (gitignored `awm/gateway/dev/.env`); per-effort dev-sandbox isolation is a separate deferred migration (owned by another agent).
EOF
  note "WEB_GAMES_ON_AWM.md: appended dedup update"
}

add_rlm_prefix_row
append_spec_update
note "done."
[[ $DRY_RUN == 1 ]] && note "(dry-run — nothing was modified)"
note "Reminder: commit WORKSPACE.md on release (or merge feat→dev→release) and WEB_GAMES_ON_AWM.md in game-bot — ONLY while agents are paused."

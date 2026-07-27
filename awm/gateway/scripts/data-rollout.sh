#!/usr/bin/env bash
# One-shot switch: ship the git-annex data layer to prod and flip a tier of
# projects onto it.
#
# WHY THIS IS A SCRIPT AND NOT A RUNBOOK
# The only step that needs *all* work paused is the gateway restart (~20s) —
# every project conversion after it only needs that one project's scopes idle.
# But a pause is a pause, so this script front-loads everything that is cheap
# and safe into the same window and stops well short of the expensive ones.
#
# ORDER MATTERS, AND ONLY IN ONE DIRECTION
# Deploy BEFORE converting anything. A converted project under old prod code is
# a half-state: `scope_create` still symlinks `.awm/data` into what is now an
# annex working tree, so a scope reads real data but gets no isolation and
# finds its large files read-only. Converting after the deploy has no such
# window. There is no ordering hazard the other way — unconverted projects
# under new code take the legacy symlink path and behave exactly as today.
#
#   ./data-rollout.sh --check          # read-only: plan + preflight, changes nothing
#   ./data-rollout.sh                  # the real switch (default tier)
#   ./data-rollout.sh --tier medium    # a later, out-of-window tier
#   ./data-rollout.sh --only switch    # deploy only, convert nothing
#   ./data-rollout.sh --rollback       # undo the deploy (see LIMITS below)
#
# LIMITS OF --rollback
# It reverts the release branch and redeploys. It does NOT un-convert projects,
# because that is per-project and wants eyes on it:
#     rm -rf <workspace>/data/<project>/.git && awm scope heal --project <p>
# Conversion is additive and in place — the files never move out of the
# directory — so that really is the whole undo.
set -euo pipefail

WORKSPACE="${AWM_WORKSPACE:-/home/tony/agentic_workspace}"
export AWM_WORKSPACE="$WORKSPACE"
AWM_ENV_BIN=/home/tony/lib/miniforge3/envs/awm/bin
AWM="$AWM_ENV_BIN/awm"
PY="$AWM_ENV_BIN/python"
BASE_URL="http://127.0.0.1:${AWM_PORT:-7819}"
ANNEX_BIN="${AWM_ANNEX_BIN:-/home/tony/lib/miniforge3/envs/annex/bin/git-annex}"
export AWM_ANNEX_BIN="$ANNEX_BIN"

# The branch prepared out-of-band (release + the data-layer commits, already
# test-run). The switch is a fast-forward onto it, so the window carries no
# merge, no conflict resolution, and no build.
RC_BRANCH="${RC_BRANCH:-deploy/data-annex}"
STATE_DIR="$WORKSPACE/.awm/state"
ROLLBACK_FILE="$STATE_DIR/data-rollout-rollback.env"

# --- tiers -----------------------------------------------------------------
# Default = the long tail: every project whose data is small enough that the
# whole convert+heal round trip is seconds. It flips ~20 real projects and
# proves the layer on live scopes without touching a single heavy dataset.
TIER_DEFAULT="mitacs-purify deep_learning_experiments odysseus job_seeking \
drawio market_monitor synclust threejs-scene-manager rt-stock gapseq \
figure-compiler game-bot container_builds virtual-auth scratch cleanproj \
cloneproj newproj lighting_system vpn_bounce _vagrant"
# Real data, many scopes each — minutes, not seconds. Run these one at a time,
# reading the report between each. Not window work.
TIER_MEDIUM="external reference research self-improvement awm"
# Tens of GB and/or six-figure file counts. Each is its own sitting, and each
# needs its broken symlinks swept FIRST (see --check output).
TIER_LARGE="spanish-lakes metasmith-libraries asv_task fabfos cyanoverse metasmith avarice"
# 199 GB, 103k files, the origin of every hard problem in the survey.
TIER_SCADC="scadc"

MODE=run
TIER=default
ONLY=all
FORCE_BROKEN=0
for a in "$@"; do
  case "$a" in
    --check)    MODE=check ;;
    --rollback) MODE=rollback ;;
    --tier=*)   TIER="${a#--tier=}" ;;
    --tier)     echo "use --tier=<name>" >&2; exit 2 ;;
    --only=*)   ONLY="${a#--only=}" ;;
    --force-broken) FORCE_BROKEN=1 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

case "$TIER" in
  default) PROJECTS="$TIER_DEFAULT" ;;
  medium)  PROJECTS="$TIER_MEDIUM" ;;
  large)   PROJECTS="$TIER_LARGE" ;;
  scadc)   PROJECTS="$TIER_SCADC" ;;
  none)    PROJECTS="" ;;
  *)       PROJECTS="$TIER" ;;   # an explicit space-separated project list
esac

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m   %s\n' "$*"; }
bad()  { printf '   \033[31mFAIL\033[0m %s\n' "$*"; }
note() { printf '        %s\n' "$*"; }

# ---------------------------------------------------------------------------
# Phase 0 — preflight. Read-only. Every one of these has bitten before.
# ---------------------------------------------------------------------------
preflight() {
  local fail=0
  say "preflight"

  [ -x "$ANNEX_BIN" ] \
    && ok "git-annex $("$ANNEX_BIN" version --raw 2>/dev/null)" \
    || { bad "no git-annex at $ANNEX_BIN"; fail=1; }

  # Hardlinking is what makes isolation free. Across a filesystem boundary
  # git-annex silently falls back to real copies and every scope pays full
  # price for its data — silently, which is the problem.
  local d p
  d=$(stat -c %d "$WORKSPACE/data" 2>/dev/null || echo x)
  p=$(stat -c %d "$WORKSPACE/projects" 2>/dev/null || echo y)
  [ "$d" = "$p" ] \
    && ok "data/ and projects/ on one filesystem (hardlinks work)" \
    || { bad "data/ and projects/ on DIFFERENT filesystems — clones would be full copies"; fail=1; }

  git -C "$WORKSPACE" rev-parse --verify -q "$RC_BRANCH" >/dev/null \
    && ok "release candidate $RC_BRANCH = $(git -C "$WORKSPACE" rev-parse --short "$RC_BRANCH")" \
    || { bad "$RC_BRANCH does not exist — prepare it before the window"; fail=1; }

  # A fast-forward is the whole point: no merge in the window.
  if git -C "$WORKSPACE" merge-base --is-ancestor HEAD "$RC_BRANCH" 2>/dev/null; then
    ok "release fast-forwards to $RC_BRANCH ($(git -C "$WORKSPACE" rev-list --count HEAD.."$RC_BRANCH") commit(s))"
  else
    bad "release does NOT fast-forward to $RC_BRANCH — re-prepare it"; fail=1
  fi

  if [ -z "$(git -C "$WORKSPACE" status --porcelain)" ]; then
    ok "release worktree clean"
  else
    bad "release worktree DIRTY — the fast-forward would refuse"
    git -C "$WORKSPACE" status --short | head -10 | sed 's/^/        /'
    fail=1
  fi

  sudo -n true 2>/dev/null \
    && ok "passwordless sudo (needed to restart awm.service)" \
    || { bad "sudo -n fails — awm deploy cannot restart the unit"; fail=1; }

  curl -sf --max-time 4 "$BASE_URL/hub/services" >/dev/null \
    && ok "gateway responding on $BASE_URL" \
    || { bad "gateway not responding on $BASE_URL"; fail=1; }

  # An 'active' scope is a running job; conversion rewrites its tree into
  # read-only symlinks underneath it.
  local busy
  busy=$("$PY" - <<'EOF' 2>/dev/null || echo ERR
import os, sqlite3
db = os.path.join(os.environ["AWM_WORKSPACE"], ".awm/services/scopes/scopes.db")
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
rows = c.execute("SELECT p.name || '/' || a.scope FROM agents a "
                 "JOIN projects p ON p.id = a.project_id "
                 "WHERE a.status='active'").fetchall()
print(" ".join(r[0] for r in rows))
EOF
)
  [ -z "$busy" ] && ok "no scope in 'active' state" || { bad "ACTIVE scopes: $busy"; fail=1; }

  return $fail
}

# Per-project readiness: what the conversion would refuse on.
survey_tier() {
  say "tier '$TIER' — ${PROJECTS:-（none）}"
  local total_broken=0 p
  for p in $PROJECTS; do
    local dir="$WORKSPACE/data/$p"
    if [ ! -d "$dir" ]; then note "$(printf '%-26s' "$p") (no data dir — will be created empty)"; continue; fi
    local n_files n_broken sz state
    n_files=$(find "$dir" -type f -not -path '*/.git/*' 2>/dev/null | wc -l)
    n_broken=$(find "$dir" -xtype l -not -path '*/.git/*' 2>/dev/null | wc -l)
    sz=$(du -sh --exclude=.git "$dir" 2>/dev/null | cut -f1)
    state=$([ -d "$dir/.git/annex" ] && echo "ALREADY ANNEX" || echo "raw")
    total_broken=$((total_broken + n_broken))
    printf '        %-26s %6s %7s files  broken=%-5s %s\n' "$p" "$sz" "$n_files" "$n_broken" "$state"
  done
  if [ "$total_broken" -gt 0 ] && [ "$FORCE_BROKEN" = 0 ]; then
    echo
    bad "$total_broken broken symlink(s) in this tier"
    note "After conversion every annexed file IS a symlink, so pre-existing rot"
    note "becomes permanently indistinguishable from 'content not fetched yet'."
    note "Sweep them, or re-run with --force-broken to accept that."
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Phase 1 — the switch. This is the only part that needs work paused.
# ---------------------------------------------------------------------------
do_switch() {
  say "switch"
  local before
  before=$(git -C "$WORKSPACE" rev-parse HEAD)
  mkdir -p "$STATE_DIR"
  printf 'RELEASE_BEFORE=%s\nAT=%s\n' "$before" "$(date -Iseconds)" > "$ROLLBACK_FILE"
  note "rollback point $before recorded in $ROLLBACK_FILE"

  git -C "$WORKSPACE" merge --ff-only "$RC_BRANCH"
  ok "release -> $(git -C "$WORKSPACE" rev-parse --short HEAD)"

  # Editable installs + an unchanged dist set + unchanged page source mean this
  # is restart + reap + verify only. No pip, no npm, no build.
  "$AWM" deploy
  ok "deploy returned clean"
}

# ---------------------------------------------------------------------------
# Phase 2 — verify the new surface is actually live before converting anything.
# ---------------------------------------------------------------------------
do_verify_surface() {
  say "verify surface"
  local fail=0 tools
  tools=$(curl -sf --max-time 8 "$BASE_URL/tools" || echo '{}')
  local t
  for t in scope_heal scope_data_status scope_data_snapshot scope_data_promote project_data_init; do
    if printf '%s' "$tools" | grep -q "\"$t\""; then ok "$t live"; else bad "$t MISSING"; fail=1; fi
  done

  # Resolution must work from the *service's* environment, not this shell's —
  # systemd hands it a minimal PATH and git-annex lives in its own mamba env.
  if "$PY" -c "
import sys; sys.path.insert(0, '$WORKSPACE/awm/services/scopes')
from awm.scopes import data_annex as da
b = da.annex_bin()
print(b or 'NONE'); sys.exit(0 if b else 1)
" >/dev/null 2>&1; then
    ok "data_annex resolves git-annex ($("$PY" -c "
import sys; sys.path.insert(0,'$WORKSPACE/awm/services/scopes')
from awm.scopes import data_annex as da; print(da.annex_bin())" 2>/dev/null))"
  else
    bad "data_annex.annex_bin() returned None — every project would fall back to the symlink"
    fail=1
  fi
  return $fail
}

# ---------------------------------------------------------------------------
# Phase 3 — convert. Per project, sequentially, stopping on the first failure.
# ---------------------------------------------------------------------------
do_convert() {
  say "convert tier '$TIER'"
  local p rc converted=() failed=()
  for p in $PROJECTS; do
    printf '\n\033[1m-- %s\033[0m\n' "$p"
    set +e
    "$PY" -m awm.scopes.scripts.migrate_data "$p" \
      $([ "$FORCE_BROKEN" = 1 ] && echo --force-broken) 2>&1 | sed 's/^/   /'
    rc=${PIPESTATUS[0]}
    set -e
    if [ "$rc" = 0 ]; then converted+=("$p"); else failed+=("$p"); bad "$p exited $rc"; fi
  done
  printf 'CONVERTED=%s\n' "${converted[*]-}" >> "$ROLLBACK_FILE"
  say "convert summary"
  ok "converted: ${converted[*]:-(none)}"
  [ ${#failed[@]} -eq 0 ] || { bad "failed: ${failed[*]}"; return 1; }
  return 0
}

do_rollback() {
  say "rollback"
  [ -f "$ROLLBACK_FILE" ] || { bad "no rollback file at $ROLLBACK_FILE"; exit 1; }
  # shellcheck disable=SC1090
  . "$ROLLBACK_FILE"
  note "reverting release to $RELEASE_BEFORE (recorded $AT)"
  git -C "$WORKSPACE" reset --hard "$RELEASE_BEFORE"
  "$AWM" deploy --no-install --no-build
  ok "release reverted and redeployed"
  if [ -n "${CONVERTED:-}" ]; then
    echo
    note "these projects are STILL converted (undo is per-project and deliberate):"
    for p in $CONVERTED; do
      note "  rm -rf $WORKSPACE/data/$p/.git && $AWM scope heal --project $p"
    done
  fi
}

# ---------------------------------------------------------------------------
main() {
  printf '\033[1mawm data-annex rollout\033[0m  workspace=%s  tier=%s  mode=%s\n' \
    "$WORKSPACE" "$TIER" "$MODE"

  if [ "$MODE" = rollback ]; then do_rollback; exit 0; fi

  preflight || { echo; bad "preflight failed — not proceeding"; exit 1; }
  survey_tier || { echo; bad "tier not ready"; exit 1; }

  if [ "$MODE" = check ]; then
    say "check complete — nothing was changed"
    note "run without --check to switch"
    exit 0
  fi

  [ "$ONLY" = convert ] || do_switch
  [ "$ONLY" = convert ] || do_verify_surface || { bad "surface verify failed — NOT converting"; exit 1; }
  if [ "$ONLY" != switch ] && [ -n "$PROJECTS" ]; then do_convert; fi

  say "done"
  note "next: ./data-rollout.sh --check --tier=medium   (out of window, one at a time)"
  note "backup: add awm/gateway/scripts/data-backup.sh to cron once a tier holds real data"
}

main

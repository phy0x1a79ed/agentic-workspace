#!/usr/bin/env bash
# Nightly off-site mirror of <workspace>/data/ via Globus.
#
# WHY A DUMB MIRROR IS SAFE
# git-annex object files are named by their own SHA256 and are never mutated in
# place — the content store is append-only and content-addressed. A partial or
# concurrent transfer can therefore only leave the mirror LAGGING, never
# corrupted, and the far side needs no annex awareness at all.
#
# WHAT IT DOES NOT CARRY
# Secrets are gitignored in each canonical data repo, but they are still on
# local disk — and this mirrors the *filesystem*, not the repository. So the
# excludes below are load-bearing, not decorative: they are what keeps
# credentials off-site. Verify with `globus ls` after a run, never by trusting
# this comment.
#
#   ./data-backup.sh              # submit + wait
#   ./data-backup.sh --no-wait    # submit and return immediately (cron)
#   ./data-backup.sh --dry-run    # print what would be submitted
set -euo pipefail

# Local Globus Connect Personal endpoint. Shares ~/, so its paths are /~/...
SRC_EP=1a8bdb3c-ee2c-11ee-9046-1f4fee4027cc
# Destination guest collection + path.
DST_EP=2602486c-1e0f-47a0-be15-eec1b0ff0f96
DST_PATH=/Workspace_backups/Tony_Liu/data/

WORKSPACE="${AWM_WORKSPACE:-$HOME/agentic_workspace}"
SRC_DIR="$WORKSPACE/data"

# The `globus` CLI lives in the awm mamba env, not on the base PATH.
export PATH=/home/tony/lib/miniforge3/envs/awm/bin:$PATH
GCP=/home/tony/lib/globusconnectpersonal-3.2.3/globusconnectpersonal

WAIT=1
DRY=0
for a in "$@"; do
  case "$a" in
    --no-wait) WAIT=0 ;;
    --dry-run) DRY=1 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

[ -d "$SRC_DIR" ] || { echo "no data dir at $SRC_DIR" >&2; exit 1; }
SRC_PATH="/~/${SRC_DIR#"$HOME"/}/"

# Never leaves this machine. Mirrors the ignore rules the canonical data repos
# carry, because a filesystem mirror does not read .gitignore.
EXCLUDES=(
  --exclude 'secrets'
  --exclude '.env'
  --exclude '.env.*'
  --exclude '.credentials.json'
  --exclude '*.pem'
  --exclude 'id_rsa*'
  # Vendored third-party checkouts are pinned in VENDORED.tsv and re-clonable.
  --exclude '.git'
)

if [ "$DRY" = "1" ]; then
  echo "would transfer: $SRC_EP:$SRC_PATH -> $DST_EP:$DST_PATH"
  printf '  %s\n' "${EXCLUDES[@]}"
  exit 0
fi

# GCP must be running or the local endpoint is unreachable.
if ! "$GCP" -status 2>/dev/null | grep -qi 'connected'; then
  echo "Globus Connect Personal not connected — starting it"
  nohup "$GCP" -start >/tmp/gcp-start.log 2>&1 &
  for _ in $(seq 1 20); do
    sleep 3
    "$GCP" -status 2>/dev/null | grep -qi 'connected' && break
  done
fi

# --sync-level checksum is what makes repeat runs free: unchanged content is
# checked and skipped, so a nightly job moves only the day's deltas.
#
# NOTE: deliberately NO --delete-destination-extra. This is a real backup
# location; pruning the mirror must be an explicit, separately reviewed action,
# never a side effect of the nightly job.
out=$(globus transfer --recursive --sync-level checksum \
        "${EXCLUDES[@]}" \
        --label "awm-data-mirror-$(date +%Y%m%d)" \
        "$SRC_EP:$SRC_PATH" "$DST_EP:$DST_PATH" 2>&1)
echo "$out"
tid=$(echo "$out" | grep -oP 'Task ID: \K\S+' || true)

if [ "$WAIT" = "1" ] && [ -n "$tid" ]; then
  globus task wait "$tid" --timeout 7200
  globus task show "$tid" | grep -E 'Status|^Files:|Bytes Transferred|Faults'
fi
echo "submitted $SRC_PATH -> $DST_PATH at $(date -Iseconds)"

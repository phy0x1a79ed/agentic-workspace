#!/usr/bin/env bash
#
# backup_to_chinook.sh — daily full-mirror backup of agentic_workspace to
# chinook over Globus.
#
# SUPERSEDED by the `dvc` service: `awm dvc mirror`, which builds a verified-
# identical transfer document (same 268 exclusions, same 950 items) and adds
# what a script in this directory cannot have — config that is not hardcoded to
# one node, and a refusal to stack a second destructive mirror on one still in
# flight. Kept only until the agentic-workspace-backup systemd unit's ExecStart
# is repointed at the CLI; see awm/services/dvc/INSTALL.md. Do not extend this.
#
# Mirrors $WS_ROOT to the destination collection with delete_destination_extra
# (deleted locally => deleted remotely, so the remote doesn't accumulate), while
# excluding DVC cache-checkouts: any output tracked by a *.dvc file is a hardlink
# into data/.dvc_cache and is fully recoverable from it via `dvc checkout`, so
# it's redundant to also mirror it — cuts remote footprint from a naive ~665GB
# (Globus can't preserve hardlinks) toward the ~188GB physical size.
# data/.dvc_cache itself is NOT excluded — it's the canonical store the
# exclusions depend on for recoverability.
#
# Submission goes through the raw Transfer API (`globus api transfer post
# /transfer`) rather than the `globus transfer` CLI subcommand, and excludes
# are done by precise path-partitioning (see _dvc_exclude_transfer_items.py),
# not Globus filter_rules: filter_rules only match by item *name* at any
# depth, and DVC output names (e.g. "assembly", "hosts", "genomes") collide
# with unrelated real content elsewhere in the tree — a name-based exclude
# would silently drop things like projects/metasmith-libraries/*/transforms/assembly
# (actual source code) right along with the DVC checkout that shares the name.
#
# skip_source_errors is on: agentic_workspace is a live workspace other agents
# are actively editing, so a file can vanish between the tree walk above and
# when Globus actually reads it. Without this, one such race retries forever
# instead of the run completing around it.
#
# Usage:
#   ./backup_to_chinook.sh --dry-run    # print the transfer document, submit nothing
#   ./backup_to_chinook.sh              # submit the transfer and wait for it to finish
#
set -euo pipefail

WS_ROOT="${AWM_WORKSPACE_ROOT:-/home/tony/agentic_workspace}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GLOBUS="${GLOBUS_BIN:-/home/tony/lib/miniforge3/envs/globus/bin/globus}"
MAMBA="${MAMBA_BIN:-/home/tony/lib/miniforge3/bin/mamba}"
SRC_EP="57b23332-9048-11f1-ad24-02ce27bde401"   # local GCP endpoint "Altair"
DST_EP="2602486c-1e0f-47a0-be15-eec1b0ff0f96"   # chinook collection
DST_PATH="/Workspace_backups/Tony_Liu/altair"
LOCK_FILE="/tmp/agentic_workspace_backup.lock"

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

note() { printf '[backup] %s\n' "$*" >&2; }

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  note "another run is already in progress (lock: $LOCK_FILE) — exiting"
  exit 1
fi

# --- Build the exclude list from DVC output pointers -------------------------
# Every *.dvc *file* (not the .dvc *directories* DVC also uses for per-repo
# config) names one cache-checkout output via its `path:` field, relative to
# the .dvc file's own directory. Resolve each to an absolute path — these are
# exact paths to partition around, not name globs.
note "discovering DVC cache-checkout paths to exclude..."
mapfile -t EXCLUDE_PATHS < <(
  find "$WS_ROOT" -name '*.dvc' -type f -print0 |
  while IFS= read -r -d '' dvcfile; do
    dvcdir="$(dirname "$dvcfile")"
    awk '/^[^ ]/{f=0} /^- /{f=1} f && /path:/{ sub(/^[ ]*path:[ ]*/, ""); print; exit}' "$dvcfile" |
    while IFS= read -r relpath; do
      printf '%s/%s\n' "$dvcdir" "$relpath"
    done
  done | sort -u
)
note "found ${#EXCLUDE_PATHS[@]} DVC-tracked output paths to exclude"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT
EXCLUDE_JSON_FILE="$WORK_DIR/exclude.json"
ITEMS_JSON_FILE="$WORK_DIR/items.json"
BODY_FILE="$WORK_DIR/body.json"

printf '%s\n' "${EXCLUDE_PATHS[@]}" | jq -R '.' | jq -s '.' >"$EXCLUDE_JSON_FILE"

note "partitioning tree around exclusions..."
"$MAMBA" run -n globus python3 "$SCRIPT_DIR/_dvc_exclude_transfer_items.py" "$WS_ROOT" "$DST_PATH" \
  <"$EXCLUDE_JSON_FILE" >"$ITEMS_JSON_FILE"
ITEM_COUNT="$(jq 'length' "$ITEMS_JSON_FILE")"
note "transfer will use $ITEM_COUNT top-level items"

if [[ "$DRY_RUN" -eq 1 ]]; then
  SUBMISSION_ID="dry-run-not-submitted"
else
  note "requesting submission id..."
  SUBMISSION_ID="$("$GLOBUS" api transfer get /submission_id --jmespath value --format unix)"
fi

jq -n \
  --arg src_ep "$SRC_EP" \
  --arg dst_ep "$DST_EP" \
  --arg label "agentic_workspace daily" \
  --arg subid "$SUBMISSION_ID" \
  --slurpfile items "$ITEMS_JSON_FILE" \
  '{
    DATA_TYPE: "transfer",
    submission_id: $subid,
    source_endpoint: $src_ep,
    destination_endpoint: $dst_ep,
    label: $label,
    sync_level: 2,
    delete_destination_extra: true,
    verify_checksum: true,
    skip_source_errors: true,
    DATA: [ $items[0][] | . + {DATA_TYPE: "transfer_item"} ]
  }' >"$BODY_FILE"

if [[ "$DRY_RUN" -eq 1 ]]; then
  note "dry run — transfer document that would be submitted:"
  jq . "$BODY_FILE"
  exit 0
fi

note "submitting transfer..."
TASK_ID="$("$GLOBUS" api transfer post /transfer --body-file "$BODY_FILE" --jmespath 'task_id' --format unix)"
note "submitted task $TASK_ID — waiting for completion..."

if "$GLOBUS" task wait "$TASK_ID" --polling-interval 30; then
  note "task $TASK_ID completed successfully"
else
  note "task $TASK_ID did not complete successfully"
  "$GLOBUS" task show "$TASK_ID"
  exit 1
fi

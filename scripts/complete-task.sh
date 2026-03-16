#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

usage() {
  echo "Usage: $(basename "$0") <project> <task> [--merge]"
  echo ""
  echo "Mark a task as completed and optionally merge its branch."
  echo ""
  echo "Options:"
  echo "  --merge   Merge the feature branch into main and push"
  exit 1
}

if [[ $# -lt 2 ]]; then
  echo "Error: project and task names are required"
  usage
fi

PROJECT=""
TASK=""
DO_MERGE=false

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --merge)
      DO_MERGE=true
      shift
      ;;
    -h|--help)
      usage
      ;;
    -*)
      echo "Error: Unknown option $1"
      exit 1
      ;;
    *)
      if [[ -z "$PROJECT" ]]; then
        PROJECT="$1"
      elif [[ -z "$TASK" ]]; then
        TASK="$1"
      else
        echo "Error: Unexpected argument '$1'"
        exit 1
      fi
      shift
      ;;
  esac
done

if [[ -z "$PROJECT" || -z "$TASK" ]]; then
  echo "Error: Both project and task names are required"
  usage
fi

BARE_DIR="$WORKSPACE_ROOT/tasks/$PROJECT/.bare"
WORKTREE_DIR="$WORKSPACE_ROOT/tasks/$PROJECT/$TASK"
FEATURE_BRANCH="feat/$TASK"
ACTIVE_LINK="$WORKSPACE_ROOT/tasks_active/${PROJECT}_${TASK}"

# Validate worktree exists
if [[ ! -d "$WORKTREE_DIR" ]]; then
  echo "Error: Task worktree not found at $WORKTREE_DIR"
  exit 1
fi

if [[ ! -d "$BARE_DIR" ]]; then
  echo "Error: Bare repo not found at $BARE_DIR"
  exit 1
fi

echo "=== Completing Task: $PROJECT/$TASK ==="

# Update .status to completed
echo "completed" > "$WORKTREE_DIR/.status"
echo "  Status updated to: completed"

# Remove tasks_active symlink
if [[ -L "$ACTIVE_LINK" ]]; then
  rm "$ACTIVE_LINK"
  echo "  Removed active symlink: tasks_active/${PROJECT}_${TASK}"
elif [[ -e "$ACTIVE_LINK" ]]; then
  rm "$ACTIVE_LINK"
  echo "  Removed active entry: tasks_active/${PROJECT}_${TASK}"
else
  echo "  No active symlink found (already inactive)"
fi

# Merge if requested
if [[ "$DO_MERGE" == true ]]; then
  echo "  Merging $FEATURE_BRANCH into main..."

  # Detect default branch
  DEFAULT_BRANCH=""
  if git -C "$BARE_DIR" rev-parse --verify main &>/dev/null; then
    DEFAULT_BRANCH="main"
  elif git -C "$BARE_DIR" rev-parse --verify master &>/dev/null; then
    DEFAULT_BRANCH="master"
  else
    echo "Error: Could not detect default branch"
    exit 1
  fi

  # Use the main worktree for merge operations
  MAIN_WORKTREE="$WORKSPACE_ROOT/tasks/$PROJECT/$DEFAULT_BRANCH"
  if [[ ! -d "$MAIN_WORKTREE" ]]; then
    echo "Error: Main worktree not found at $MAIN_WORKTREE"
    echo "Cannot merge without the main worktree"
    exit 1
  fi

  git -C "$MAIN_WORKTREE" checkout "$DEFAULT_BRANCH"
  git -C "$MAIN_WORKTREE" merge "$FEATURE_BRANCH" -m "Merge $FEATURE_BRANCH into $DEFAULT_BRANCH"
  echo "  Merged $FEATURE_BRANCH into $DEFAULT_BRANCH"

  # Push (don't fail if no remote)
  git -C "$MAIN_WORKTREE" push 2>/dev/null && \
    echo "  Pushed to remote" || \
    echo "  Warning: Push failed or no remote configured"
fi

# Append completion entry to experiences.md
COMPLETION_DATE=$(date +%Y-%m-%d)
EXPERIENCES_FILE="$WORKTREE_DIR/experiences.md"
if [[ -f "$EXPERIENCES_FILE" ]]; then
  cat >> "$EXPERIENCES_FILE" <<EOF

## Completed: $COMPLETION_DATE

- Task marked as completed on $COMPLETION_DATE
- Branch: $FEATURE_BRANCH
$(if [[ "$DO_MERGE" == true ]]; then echo "- Merged into $DEFAULT_BRANCH"; fi)
EOF
  echo "  Appended completion entry to experiences.md"
else
  echo "  Warning: experiences.md not found, skipping"
fi

echo ""
echo "=== Completion Summary ==="
echo "  Project:  $PROJECT"
echo "  Task:     $TASK"
echo "  Status:   completed"
echo "  Merged:   $DO_MERGE"
echo "  Date:     $COMPLETION_DATE"

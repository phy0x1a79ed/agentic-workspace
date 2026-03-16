#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

usage() {
  echo "Usage: $(basename "$0") <project> <task> [--from <branch>]"
  echo ""
  echo "Create a new task worktree branching from the specified branch."
  echo ""
  echo "Options:"
  echo "  --from <branch>   Base branch (default: main or master)"
  exit 1
}

if [[ $# -lt 2 ]]; then
  echo "Error: project and task names are required"
  usage
fi

PROJECT=""
TASK=""
FROM_BRANCH=""

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from)
      [[ $# -lt 2 ]] && { echo "Error: --from requires a branch name"; exit 1; }
      FROM_BRANCH="$2"
      shift 2
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

# Validate project exists
if [[ ! -d "$BARE_DIR" ]]; then
  echo "Error: Project '$PROJECT' not found (expected $BARE_DIR)"
  echo "Create it first with: scripts/new-project.sh $PROJECT"
  exit 1
fi

# Detect default branch if --from not specified
if [[ -z "$FROM_BRANCH" ]]; then
  if git -C "$BARE_DIR" rev-parse --verify main &>/dev/null; then
    FROM_BRANCH="main"
  elif git -C "$BARE_DIR" rev-parse --verify master &>/dev/null; then
    FROM_BRANCH="master"
  else
    echo "Error: Could not detect default branch (neither main nor master found)"
    echo "Specify explicitly with --from <branch>"
    exit 1
  fi
fi

WORKTREE_DIR="$WORKSPACE_ROOT/tasks/$PROJECT/$TASK"
FEATURE_BRANCH="feat/$TASK"

# Check if worktree already exists
if [[ -d "$WORKTREE_DIR" ]]; then
  echo "Error: Task worktree already exists at $WORKTREE_DIR"
  exit 1
fi

echo "=== Creating Task: $PROJECT/$TASK ==="
echo "  Base branch: $FROM_BRANCH"
echo "  Feature branch: $FEATURE_BRANCH"

# Create worktree with feature branch
git -C "$BARE_DIR" worktree add "../$TASK" -b "$FEATURE_BRANCH" "$FROM_BRANCH"
echo "  Created worktree: tasks/$PROJECT/$TASK"

# Create results directory for this task
mkdir -p "$WORKSPACE_ROOT/results/$PROJECT/$TASK"
echo "  Created results/$PROJECT/$TASK/"

# Create symlinks in the worktree
ln -sfn "$WORKSPACE_ROOT/data" "$WORKTREE_DIR/data"
echo "  Symlinked data -> $WORKSPACE_ROOT/data"

ln -sfn "$WORKSPACE_ROOT/results/$PROJECT/$TASK" "$WORKTREE_DIR/results"
echo "  Symlinked results -> $WORKSPACE_ROOT/results/$PROJECT/$TASK"

ln -sfn "$WORKSPACE_ROOT/reports/$PROJECT" "$WORKTREE_DIR/reports"
echo "  Symlinked reports -> $WORKSPACE_ROOT/reports/$PROJECT"

# Write .status file
echo "active" > "$WORKTREE_DIR/.status"
echo "  Created .status (active)"

# Initialize experiences.md
cat > "$WORKTREE_DIR/experiences.md" <<EOF
# Experiences -- $PROJECT/$TASK

## Log

EOF
echo "  Created experiences.md"

# Create env/ directory with minimal environment.yml
mkdir -p "$WORKTREE_DIR/env"
cat > "$WORKTREE_DIR/env/environment.yml" <<EOF
# Per-task overlay — install into project env with:
#   mamba env update -n $PROJECT --file env/environment.yml
name: $PROJECT
channels:
  - conda-forge
  - bioconda
dependencies: []
EOF
echo "  Created env/environment.yml"

# Create symlink in tasks_active
mkdir -p "$WORKSPACE_ROOT/tasks_active"
ln -sfn "../tasks/$PROJECT/$TASK" "$WORKSPACE_ROOT/tasks_active/${PROJECT}_${TASK}"
echo "  Symlinked tasks_active/${PROJECT}_${TASK}"

echo ""
echo "=== Task Summary ==="
echo "  Project:        $PROJECT"
echo "  Task:           $TASK"
echo "  Worktree:       tasks/$PROJECT/$TASK"
echo "  Branch:         $FEATURE_BRANCH"
echo "  Results:        results/$PROJECT/$TASK"
echo "  Reports:        reports/$PROJECT"
echo "  Status:         active"
echo ""
echo "To work in this task:"
echo "  cd $WORKTREE_DIR"

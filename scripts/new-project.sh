#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

usage() {
  echo "Usage: $(basename "$0") <name> [--clone <url>] [--fork <url>]"
  echo ""
  echo "Create a new project with a bare git repo and supporting directories."
  echo ""
  echo "Options:"
  echo "  --clone <url>   Clone an existing repo (bare)"
  echo "  --fork  <url>   Fork via gh, then clone the fork (bare)"
  exit 1
}

if [[ $# -lt 1 ]]; then
  usage
fi

PROJECT_NAME=""
CLONE_URL=""
FORK_URL=""
MODE="fresh"

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --clone)
      [[ $# -lt 2 ]] && { echo "Error: --clone requires a URL"; exit 1; }
      CLONE_URL="$2"
      MODE="clone"
      shift 2
      ;;
    --fork)
      [[ $# -lt 2 ]] && { echo "Error: --fork requires a URL"; exit 1; }
      FORK_URL="$2"
      MODE="fork"
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
      if [[ -z "$PROJECT_NAME" ]]; then
        PROJECT_NAME="$1"
      else
        echo "Error: Unexpected argument '$1'"
        exit 1
      fi
      shift
      ;;
  esac
done

if [[ -z "$PROJECT_NAME" ]]; then
  echo "Error: Project name is required"
  usage
fi

BARE_DIR="$WORKSPACE_ROOT/tasks/$PROJECT_NAME/.bare"

if [[ -d "$BARE_DIR" ]]; then
  echo "Error: Project '$PROJECT_NAME' already exists at $BARE_DIR"
  exit 1
fi

echo "=== Creating Project: $PROJECT_NAME (mode: $MODE) ==="

case "$MODE" in
  fresh)
    git init --bare "$BARE_DIR"
    echo "  Initialized bare repo"

    # Attempt to create GitHub repo (don't fail if it exists)
    if command -v gh &>/dev/null; then
      echo "  Attempting to create GitHub repo phy0x1a79ed/$PROJECT_NAME ..."
      gh repo create "phy0x1a79ed/$PROJECT_NAME" --private 2>/dev/null && \
        echo "  GitHub repo created" || \
        echo "  GitHub repo already exists or creation skipped"
      # Add remote origin
      git -C "$BARE_DIR" remote add origin "https://github.com/phy0x1a79ed/$PROJECT_NAME.git" 2>/dev/null || \
        echo "  Remote origin already set"
    else
      echo "  Warning: gh CLI not found, skipping GitHub repo creation"
    fi

    # Create an initial commit so we have a main branch
    # Use a temporary worktree for this
    TEMP_DIR=$(mktemp -d)
    git clone "$BARE_DIR" "$TEMP_DIR/init" 2>/dev/null || true
    pushd "$TEMP_DIR/init" >/dev/null
    git checkout -b main 2>/dev/null || true
    git commit --allow-empty -m "Initial commit for $PROJECT_NAME"
    git push origin main 2>/dev/null || true
    popd >/dev/null
    rm -rf "$TEMP_DIR"
    echo "  Created initial commit on main"
    ;;

  clone)
    git clone --bare "$CLONE_URL" "$BARE_DIR"
    echo "  Cloned bare repo from $CLONE_URL"

    # Fix fetch refspec
    git -C "$BARE_DIR" config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
    echo "  Fixed fetch refspec"
    ;;

  fork)
    if ! command -v gh &>/dev/null; then
      echo "Error: gh CLI is required for --fork"
      exit 1
    fi

    echo "  Forking $FORK_URL ..."
    FORK_RESULT=$(gh repo fork "$FORK_URL" --clone=false 2>&1) || true
    echo "  $FORK_RESULT"

    # Determine fork URL: extract from gh output or construct it
    FORK_OWNER="phy0x1a79ed"
    REPO_NAME=$(basename "$FORK_URL" .git)
    FORK_CLONE_URL="https://github.com/$FORK_OWNER/$REPO_NAME.git"

    git clone --bare "$FORK_CLONE_URL" "$BARE_DIR"
    echo "  Cloned bare fork from $FORK_CLONE_URL"

    # Fix fetch refspec
    git -C "$BARE_DIR" config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'

    # Add upstream remote pointing to original URL
    git -C "$BARE_DIR" remote add upstream "$FORK_URL"
    echo "  Added upstream remote -> $FORK_URL"
    ;;
esac

# Create supporting directories
mkdir -p "$WORKSPACE_ROOT/data/$PROJECT_NAME/raw"
mkdir -p "$WORKSPACE_ROOT/data/$PROJECT_NAME/staged"
mkdir -p "$WORKSPACE_ROOT/results/$PROJECT_NAME"
mkdir -p "$WORKSPACE_ROOT/reports/$PROJECT_NAME"
echo "  Created data/$PROJECT_NAME/{raw,staged}, results/$PROJECT_NAME/, reports/$PROJECT_NAME/"

# Detect default branch (main or master)
DEFAULT_BRANCH=""
if git -C "$BARE_DIR" rev-parse --verify main &>/dev/null; then
  DEFAULT_BRANCH="main"
elif git -C "$BARE_DIR" rev-parse --verify master &>/dev/null; then
  DEFAULT_BRANCH="master"
else
  # Check remote refs
  if git -C "$BARE_DIR" rev-parse --verify refs/remotes/origin/main &>/dev/null; then
    DEFAULT_BRANCH="main"
  elif git -C "$BARE_DIR" rev-parse --verify refs/remotes/origin/master &>/dev/null; then
    DEFAULT_BRANCH="master"
  else
    # Fallback: check HEAD reference
    HEAD_REF=$(git -C "$BARE_DIR" symbolic-ref HEAD 2>/dev/null || true)
    if [[ "$HEAD_REF" == "refs/heads/master" ]]; then
      DEFAULT_BRANCH="master"
    else
      DEFAULT_BRANCH="main"
    fi
  fi
fi

echo "  Default branch: $DEFAULT_BRANCH"

# Create main worktree
WORKTREE_DIR="$WORKSPACE_ROOT/tasks/$PROJECT_NAME/$DEFAULT_BRANCH"
if [[ ! -d "$WORKTREE_DIR" ]]; then
  git -C "$BARE_DIR" worktree add "../$DEFAULT_BRANCH" "$DEFAULT_BRANCH" 2>/dev/null || \
    git -C "$BARE_DIR" worktree add "../$DEFAULT_BRANCH" -b "$DEFAULT_BRANCH" 2>/dev/null || \
    echo "  Warning: Could not create worktree for $DEFAULT_BRANCH"
  echo "  Created worktree: tasks/$PROJECT_NAME/$DEFAULT_BRANCH"
else
  echo "  Worktree already exists: tasks/$PROJECT_NAME/$DEFAULT_BRANCH"
fi

# Copy AGENTS.md template if it exists
TEMPLATE="$WORKSPACE_ROOT/skills/templates/AGENTS.md.template"
if [[ -f "$TEMPLATE" && -d "$WORKTREE_DIR" ]]; then
  cp "$TEMPLATE" "$WORKTREE_DIR/AGENTS.md"
  echo "  Copied AGENTS.md template into worktree"
fi

echo ""
echo "=== Project Summary ==="
echo "  Project:    $PROJECT_NAME"
echo "  Bare repo:  tasks/$PROJECT_NAME/.bare"
echo "  Worktree:   tasks/$PROJECT_NAME/$DEFAULT_BRANCH"
echo "  Data:       data/$PROJECT_NAME/{raw,staged}"
echo "  Results:    results/$PROJECT_NAME/"
echo "  Reports:    reports/$PROJECT_NAME/"
echo "  Mode:       $MODE"
echo ""
echo "Next: create a task with"
echo "  scripts/new-task.sh $PROJECT_NAME <task-name>"

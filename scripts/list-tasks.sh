#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

usage() {
  echo "Usage: $(basename "$0") [--active|--completed|--paused|--all] [--project <name>]"
  echo ""
  echo "List tasks filtered by status."
  echo ""
  echo "Options:"
  echo "  --active       Show active tasks (default)"
  echo "  --completed    Show completed tasks"
  echo "  --paused       Show paused tasks"
  echo "  --all          Show all tasks"
  echo "  --project <n>  Filter to a specific project"
  exit 1
}

FILTER="active"
PROJECT_FILTER=""

# Parse arguments
while [[ $# -gt 0 ]]; do
  case "$1" in
    --active)
      FILTER="active"
      shift
      ;;
    --completed)
      FILTER="completed"
      shift
      ;;
    --paused)
      FILTER="paused"
      shift
      ;;
    --all)
      FILTER="all"
      shift
      ;;
    --project)
      [[ $# -lt 2 ]] && { echo "Error: --project requires a name"; exit 1; }
      PROJECT_FILTER="$2"
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
      echo "Error: Unexpected argument '$1'"
      exit 1
      ;;
  esac
done

# Print header
printf "%-20s %-25s %-12s %-30s\n" "PROJECT" "TASK" "STATUS" "BRANCH"
printf "%-20s %-25s %-12s %-30s\n" "-------" "----" "------" "------"

FOUND=0

if [[ "$FILTER" == "active" ]]; then
  # Fast path: list tasks_active/ symlinks
  if [[ -d "$WORKSPACE_ROOT/tasks_active" ]]; then
    for link in "$WORKSPACE_ROOT/tasks_active"/*; do
      [[ -e "$link" || -L "$link" ]] || continue
      basename_link=$(basename "$link")

      # Extract project and task from the symlink name (PROJECT_TASK format)
      # The symlink target tells us the actual path
      target=$(readlink -f "$link" 2>/dev/null || echo "")

      if [[ -n "$target" && -d "$target" ]]; then
        # Derive project and task from the target path
        task_name=$(basename "$target")
        project_name=$(basename "$(dirname "$target")")
      else
        # Fallback: parse from symlink name (first _ separates project from task)
        project_name="${basename_link%%_*}"
        task_name="${basename_link#*_}"
      fi

      # Apply project filter
      if [[ -n "$PROJECT_FILTER" && "$project_name" != "$PROJECT_FILTER" ]]; then
        continue
      fi

      # Get branch info
      branch=""
      if [[ -d "$target" ]]; then
        branch=$(git -C "$target" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
      fi

      printf "%-20s %-25s %-12s %-30s\n" "$project_name" "$task_name" "active" "$branch"
      FOUND=$((FOUND + 1))
    done
  fi
else
  # Scan all projects and tasks
  for project_dir in "$WORKSPACE_ROOT/tasks"/*/; do
    [[ -d "$project_dir" ]] || continue
    project_name=$(basename "$project_dir")

    # Apply project filter
    if [[ -n "$PROJECT_FILTER" && "$project_name" != "$PROJECT_FILTER" ]]; then
      continue
    fi

    for task_dir in "$project_dir"*/; do
      [[ -d "$task_dir" ]] || continue
      task_name=$(basename "$task_dir")

      # Skip the .bare directory
      [[ "$task_name" == ".bare" ]] && continue

      # Read status
      status="unknown"
      if [[ -f "$task_dir/.status" ]]; then
        status=$(cat "$task_dir/.status")
      fi

      # Apply status filter
      if [[ "$FILTER" != "all" && "$status" != "$FILTER" ]]; then
        continue
      fi

      # Get branch info
      branch=$(git -C "$task_dir" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")

      printf "%-20s %-25s %-12s %-30s\n" "$project_name" "$task_name" "$status" "$branch"
      FOUND=$((FOUND + 1))
    done
  done
fi

if [[ "$FOUND" -eq 0 ]]; then
  echo "(no tasks found matching filter: $FILTER)"
fi

echo ""
echo "Total: $FOUND task(s)"

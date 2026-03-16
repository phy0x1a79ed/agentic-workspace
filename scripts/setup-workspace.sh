#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Workspace Bootstrap ==="
echo "Root: $WORKSPACE_ROOT"

# Create all required directories
dirs=(
  data/reference
  results
  reports
  tasks
  tasks_active
  skills/sops
  skills/tools
  skills/templates
  scripts
)

for d in "${dirs[@]}"; do
  if [[ ! -d "$WORKSPACE_ROOT/$d" ]]; then
    mkdir -p "$WORKSPACE_ROOT/$d"
    echo "  Created $d/"
  else
    echo "  Exists  $d/"
  fi
done

# Init git if not already a repo
if [[ ! -d "$WORKSPACE_ROOT/.git" ]]; then
  git -C "$WORKSPACE_ROOT" init
  echo "  Initialized git repository"
else
  echo "  Git repo already initialized"
fi

# Create .gitignore if missing
if [[ ! -f "$WORKSPACE_ROOT/.gitignore" ]]; then
  cat > "$WORKSPACE_ROOT/.gitignore" <<'GITIGNORE'
# Workspace runtime directories (not tracked)
data/
results/
reports/
tasks/
tasks_active/

# Python
__pycache__/
*.pyc
*.pyo
*.egg-info/
.eggs/

# Data files
*.parquet
*.pkl
*.tar.gz
*.h5
*.hdf5

# Editor/IDE
.vscode/
.idea/
*.swp
*.swo
*~

# OS
.DS_Store
Thumbs.db
GITIGNORE
  echo "  Created .gitignore"
else
  echo "  .gitignore already exists"
fi

# Make initial commit if no commits exist
if ! git -C "$WORKSPACE_ROOT" rev-parse HEAD &>/dev/null; then
  git -C "$WORKSPACE_ROOT" add -A
  git -C "$WORKSPACE_ROOT" commit -m "Initial workspace setup" --allow-empty
  echo "  Created initial commit"
else
  echo "  Commits already exist"
fi

# Print status
echo ""
echo "=== Workspace Status ==="
echo "Directory tree:"
for d in "${dirs[@]}"; do
  count=$(find "$WORKSPACE_ROOT/$d" -maxdepth 1 -mindepth 1 2>/dev/null | wc -l)
  printf "  %-30s %d items\n" "$d/" "$count"
done
echo ""
echo "Git status:"
git -C "$WORKSPACE_ROOT" log --oneline -5 2>/dev/null || echo "  No commits yet"
echo ""
echo "Setup complete."

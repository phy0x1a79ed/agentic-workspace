#!/usr/bin/env bash
set -euo pipefail
WORKSPACE_ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "=== AWM Setup ==="

# 1. Create mamba environment (idempotent)
if ! mamba env list 2>/dev/null | awk '{print $1}' | grep -qx "awm"; then
    echo "Creating mamba environment 'awm'..."
    mamba env create -f "$WORKSPACE_ROOT/environment.yml"
else
    echo "Updating mamba environment 'awm'..."
    mamba env update -f "$WORKSPACE_ROOT/environment.yml" --prune
fi

# 2. Install awm package in dev mode into the env
echo "Installing awm package (editable)..."
mamba run -n awm pip install -e "$WORKSPACE_ROOT" --no-deps

# 3. Create .awm/ runtime directory
mkdir -p "$WORKSPACE_ROOT/.awm"

# 4. Initialize the database
echo "Initializing database..."
mamba run -n awm python -m awm init

# 5. Create shell wrapper at ~/.local/bin/awm
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/awm" <<'WRAPPER'
#!/usr/bin/env bash
exec mamba run -n awm python -m awm "$@"
WRAPPER
chmod +x "$HOME/.local/bin/awm"

echo ""
echo "Done. 'awm' is now available (ensure ~/.local/bin is on PATH)."

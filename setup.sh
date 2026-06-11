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

# 5. Create shell wrappers at ~/.local/bin/{awm,awm-mcp}
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/awm" <<'WRAPPER'
#!/usr/bin/env bash
exec mamba run -n awm python -m awm "$@"
WRAPPER
chmod +x "$HOME/.local/bin/awm"

cat > "$HOME/.local/bin/awm-mcp" <<'WRAPPER'
#!/usr/bin/env bash
exec mamba run -n awm awm-mcp "$@"
WRAPPER
chmod +x "$HOME/.local/bin/awm-mcp"

# 6. Expose under /usr/local/bin so non-interactive shells (ssh "awm ...",
# cron, systemd's default minimal PATH) can find the binaries without
# depending on ~/.profile / ~/.bashrc PATH-extension. Symlink straight
# at the env's console-scripts — their python shebang is pinned to the
# env interpreter, so they don't need mamba on PATH to run. Needs sudo;
# skipped gracefully if unavailable.
ENV_AWM="$(mamba run -n awm bash -c 'echo $CONDA_PREFIX/bin')/awm"
ENV_AWM_MCP="$(dirname "$ENV_AWM")/awm-mcp"
if command -v sudo >/dev/null 2>&1; then
    echo "Symlinking awm + awm-mcp into /usr/local/bin (needs sudo)..."
    sudo ln -sfn "$ENV_AWM" /usr/local/bin/awm
    sudo ln -sfn "$ENV_AWM_MCP" /usr/local/bin/awm-mcp
else
    echo "(sudo not available — skipping /usr/local/bin symlinks; non-interactive shells will need PATH set manually)"
fi

echo ""
echo "Done. 'awm' is now available system-wide."

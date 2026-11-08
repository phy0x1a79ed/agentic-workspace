# Sourced by a plugin's install hook, with $ENV naming the plugin's own conda env and $HERE its folder.
if ! command -v mamba >/dev/null; then
    for conda_root in "$HOME/miniforge3" "$HOME/lib/miniforge3" /opt/miniforge3; do
        [ -x "$conda_root/bin/mamba" ] || continue
        export PATH="$conda_root/condabin:$conda_root/bin:$PATH"
        break
    done
fi
mamba run -n "$ENV" true 2>/dev/null || mamba create -y -n "$ENV" -c conda-forge python=3.12 pip

run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

# The gateway respawns a service under systemd's minimal PATH, which has no mamba.
bake_runtime_env() {
    local py
    py="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
    printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$py" "$(dirname "$py")" > "$HERE/.runtime-env"
}

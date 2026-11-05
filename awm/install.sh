#!/usr/bin/env bash
# Install awm's Python side into one env: the component libraries, then each plugin's own
# install.sh. The core is the Rust `awm` binary (core/), which this script places when
# AWM_BIN names a built one. Override the target env with AWM_ENV.
#
# It installs the plugins that the node's install config enables (`plugins` and `exclude` in
# <AWM_DIR>/config.toml), asking the core binary `awm wants`, so the gateway and this script
# read one rule. AWM_CORE names that binary (default: AWM_BIN, else ~/.local/bin/awm).
# AWM_SERVICE_ROOTS (a ':' list) adds plugin roots outside this tree. A name an earlier root
# holds is skipped, as the gateway's loader does.
set -euo pipefail
WS="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
export AWM_ENV="${AWM_ENV:-awm}"
ENV="$AWM_ENV"
run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

# Host prerequisites the plugins assume.
missing=()
command -v socat >/dev/null || missing+=("socat")
if [ ${#missing[@]} -gt 0 ]; then
    printf 'awm install: missing prerequisite: %s\n' "${missing[@]}" >&2
    echo "       sudo apt-get install -y socat" >&2
    exit 1
fi

for lib in config persistence gatewayclient grantlib claudedaemon kept trustpolicy tlsfront dvcdata; do
    [ -d "$WS/awm/service_components/$lib" ] && run "$WS/awm/service_components/$lib" --no-deps
done

export AWM_ROOT="$WS/awm"
export AWM_DIR="${AWM_DIR:-$WS/.awm}"
core="${AWM_CORE:-${AWM_BIN:-$HOME/.local/bin/awm}}"
wanted() {
    local rc=0
    "$core" wants "$1" || rc=$?
    case "$rc" in 0) return 0 ;; 1) return 1 ;; *) echo "awm install: '$core wants $1' failed (exit $rc)" >&2; exit 1 ;; esac
}
declare -A seen=()
IFS=: read -ra extra_roots <<<"${AWM_SERVICE_ROOTS:-}"
for root in "$WS/awm/services" "$WS/awm/plugins" "${extra_roots[@]}"; do
    [ -d "$root" ] || continue
    for d in "$root"/*/; do
        [ -d "$d" ] || continue
        name="$(basename "$d")"
        case "$name" in .*|_*) continue ;; esac
        [ -z "${seen[$name]:-}" ] || continue
        seen[$name]=1
        s="${d}install.sh"
        [ -f "$s" ] || continue
        if ! wanted "$name"; then
            echo "- skipping plugin (this node's config.toml does not enable it): $name"
            continue
        fi
        echo "+ installing plugin: $s"
        bash "$s"
    done
done

if [ -n "${AWM_BIN:-}" ]; then
    mkdir -p "$HOME/.local/bin"
    install -m 0755 "$AWM_BIN" "$HOME/.local/bin/awm.new"
    mv -f "$HOME/.local/bin/awm.new" "$HOME/.local/bin/awm"
    echo "+ placed $AWM_BIN as ~/.local/bin/awm"
fi

echo "Installed awm's libraries and plugins into env '$ENV'."

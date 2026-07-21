#!/usr/bin/env bash
# Canonical install for the awm-drawio service (editable, into the `awm` env),
# plus the drawio web client the editor needs.
#
# The web client is a ~150 MB tree of JS/CSS/images. It is *built*, not
# committed: this script clones upstream jgraph/drawio at a pinned tag and
# applies awm's two patches on top, so `webapp/` is reproducible from source and
# stays out of the repo. Skip it with DRAWIO_SKIP_APP=1 when you only want the
# verbs (an agent can build a diagram headlessly; only the browser editor needs
# these bytes).
#
# Override the target env with AWM_ENV=<name>.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
WS="$(git -C "$HERE" rev-parse --show-toplevel)"
ENV="${AWM_ENV:-awm}"

# Pinned to the release the prototype was built and verified against. Moving
# this means re-verifying the app.min.js patch below, whose anchor is a literal
# string inside a minified bundle.
DRAWIO_REPO="${DRAWIO_REPO:-https://github.com/jgraph/drawio.git}"
DRAWIO_TAG="${DRAWIO_TAG:-v29.6.6}"

run() { echo "+ pip install -e $*"; mamba run -n "$ENV" pip install -e "$@"; }

run "$WS/awm/service_components/config" --no-deps
run "$WS/awm/service_components/gatewayclient" --no-deps
run "$WS/awm/service_components/persistence" --no-deps
run "$WS/awm/services/drawio"

# Bake the target env's absolute interpreter into a gitignored `.runtime-env`
# sidecar so the hub supervisor can respawn this service under systemd's
# minimal PATH (no `mamba`).
PYBIN="$(mamba run -n "$ENV" python -c 'import sys; print(sys.executable)')"
printf 'AWM_PYTHON=%s\nAWM_ENV_BIN=%s\n' "$PYBIN" "$(dirname "$PYBIN")" \
    > "$HERE/.runtime-env"

# ---------------------------------------------------------------------------
# The drawio web client
# ---------------------------------------------------------------------------
APP="$HERE/webapp"
if [ "${DRAWIO_SKIP_APP:-}" = "1" ]; then
    echo "Skipping web client (DRAWIO_SKIP_APP=1); verbs only, no browser editor."
elif [ -f "$APP/index.html" ] && [ "${DRAWIO_FORCE:-}" != "1" ]; then
    echo "Web client already present at $APP (DRAWIO_FORCE=1 to rebuild)."
else
    echo "Fetching drawio $DRAWIO_TAG …"
    rm -rf "$APP" "$HERE/.drawio-src"
    git clone --depth 1 --branch "$DRAWIO_TAG" "$DRAWIO_REPO" "$HERE/.drawio-src"
    cp -a "$HERE/.drawio-src/src/main/webapp" "$APP"
    rm -rf "$HERE/.drawio-src"

    # -- patch 1: expose the EditorUi instance to page scripts --------------
    # Without this, window.__drawioUi is undefined, PreConfig.js never attaches,
    # and the editor looks fine while silently saving nothing. So the anchor
    # match is verified explicitly: a missed patch must fail the install, not
    # produce a broken editor that only reveals itself when work is lost.
    APPJS="$APP/js/app.min.js"
    ANCHOR='new App(new Editor("0"==urlParams.chrome||"min"==uiTheme,null,null,null,"0"!=urlParams.chrome));'
    HITS="$(grep -c -F "$ANCHOR" "$APPJS" || true)"
    if [ "$HITS" != "1" ]; then
        echo "FATAL: expected exactly 1 occurrence of the App-construction anchor" >&2
        echo "       in $APPJS, found $HITS. Upstream changed; re-derive the patch" >&2
        echo "       before pinning a newer DRAWIO_TAG." >&2
        exit 1
    fi
    python3 - "$APPJS" "$ANCHOR" <<'PY'
import sys
path, anchor = sys.argv[1], sys.argv[2]
src = open(path, encoding="utf-8").read()
out = src.replace(anchor, anchor + "window.__drawioUi=x;", 1)
assert out != src, "anchor matched by grep but not by python — encoding mismatch"
open(path, "w", encoding="utf-8").write(out)
PY
    grep -q "window.__drawioUi=x;" "$APPJS" \
        || { echo "FATAL: __drawioUi patch did not apply" >&2; exit 1; }
    echo "  patched app.min.js (expose window.__drawioUi)"

    # -- patch 2: awm's PreConfig -------------------------------------------
    # Replaces upstream's stub with the save/flush/push client that talks to
    # this service. Upstream loads js/PreConfig.js before the app, which is
    # exactly the hook we need.
    cp "$HERE/patches/PreConfig.js" "$APP/js/PreConfig.js"
    echo "  installed awm PreConfig.js"

    echo "Web client ready at $APP ($(du -sh "$APP" | cut -f1))."
fi

echo "Installed awm-drawio into env '$ENV'."

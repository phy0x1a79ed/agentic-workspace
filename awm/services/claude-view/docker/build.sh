#!/usr/bin/env bash
# Produce vendor/v<VERSION>/ — the runnable claude-view install — from scratch.
#
# The output directory is a hybrid, and deliberately so:
#
#   claude-view   compiled here, from the pinned upstream tag, unmodified
#   dist/         the OFFICIAL prebuilt frontend, lifted from the release tarball
#   sidecar/      likewise
#
# Only the Rust server needs rebuilding — the frontend is *not* embedded in the
# binary (crates/server/src/startup/paths.rs resolves `./dist` beside the
# executable at runtime), so we take upstream's own compiled assets rather than
# standing up a bun/node toolchain to reproduce bytes they already publish. That
# keeps this to one compiler and one container.
#
# Everything is checksum-anchored: the release tarball against upstream's
# published checksums.txt, and the source against the git tag. Nothing is
# patched. This is the same release, differing only in who ran the compiler.
#
# Usage:
#   ./build.sh              # image (if needed) → source → compile → stage
#   ./build.sh --image      # rebuild the builder image only
#   ./build.sh --clean      # drop the source tree and cargo cache, then build
set -euo pipefail
cd "$(dirname "$0")"
HERE="$PWD"
SERVICE_DIR="$(dirname "$HERE")"
VENDOR="$SERVICE_DIR/vendor"

VERSION="${CLAUDE_VIEW_VERSION:-0.45.0}"
REPO="https://github.com/tombelieber/claude-view.git"
TARBALL="claude-view-linux-x64.tar.gz"
IMAGE="awm-claude-view-builder:rust1.95-musl"

SRC="$VENDOR/src"
OUT="$VENDOR/v$VERSION"
# Cargo's download cache. A host directory rather than a named docker volume:
# the build runs as the host uid (so `target/` on the bind mount comes out
# owned by us, not root), and a fresh named volume is created root-owned, which
# that uid then cannot write. A host dir we mkdir ourselves has the right owner
# by construction, and `--clean` can just delete it.
CARGO_DIR="$VENDOR/cargo"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# --- flags -----------------------------------------------------------------
IMAGE_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --image) IMAGE_ONLY=1 ;;
        --clean) rm -rf "$SRC" "$CARGO_DIR" ;;
        *) echo "unknown flag: $arg" >&2; exit 2 ;;
    esac
done

# --- 1. builder image ------------------------------------------------------
log "builder image $IMAGE"
docker build -t "$IMAGE" "$HERE"
[ "$IMAGE_ONLY" = 1 ] && exit 0

# --- 2. upstream source, at the pinned tag ---------------------------------
# Shallow, tag-pinned, no submodules. Upstream declares a `private` submodule
# (claude-view-private) that is not public; the server target does not need it,
# and --recurse-submodules would fail on it, so it is never initialised.
log "source: $REPO @ v$VERSION"
if [ ! -d "$SRC/.git" ]; then
    rm -rf "$SRC"
    git clone --depth 1 --branch "v$VERSION" "$REPO" "$SRC"
else
    echo "already cloned; verifying tag"
fi
HEAD_SHA="$(git -C "$SRC" rev-parse HEAD)"
echo "source HEAD = $HEAD_SHA"

# Reset to the tag, then apply our patches — in that order, every time. This is
# what keeps the build honest: the tree is never *incrementally* modified, so
# what gets compiled is always exactly "tag + the patches in patches/", and a
# hand-edit made while debugging cannot survive into a build. `clean -fd` drops
# stray untracked files but leaves gitignored ones (notably target/) alone, so
# the incremental build cache survives.
log "resetting to v$VERSION and applying patches"
git -C "$SRC" reset --hard "$HEAD_SHA" >/dev/null
git -C "$SRC" clean -fd >/dev/null

PATCHES=()
if compgen -G "$HERE/patches/*.patch" >/dev/null; then
    for p in "$HERE"/patches/*.patch; do
        echo "  applying $(basename "$p")"
        git -C "$SRC" apply --whitespace=nowarn "$p"
        PATCHES+=("$(basename "$p")")
    done
fi
[ ${#PATCHES[@]} -eq 0 ] && echo "  (none)"

# Whatever the patches touched is now the *only* difference from the tag.
# Record it so BUILD_INFO can state precisely what was changed.
PATCHED_FILES="$(git -C "$SRC" diff --name-only | tr '\n' ' ')"

# --- 3. compile ------------------------------------------------------------
# The exact command upstream's release.yml runs, minus the secret-injected env
# (POSTHOG_API_KEY / SUPABASE_* / RELAY_URL / SHARE_*). Those are read via
# option_env! at compile time, so omitting them does not fail the build — it
# makes telemetry, cloud sync, relay and share structurally inert. That is a
# feature here, not a gap: telemetry cannot be re-enabled by an env var because
# there is no key compiled in to enable.
#
# The toolchain itself is upstream's: the repo carries a rust-toolchain.toml
# pinning channel 1.94.1, and rustup inside the container honours it, fetching
# the musl-host build of that exact compiler. So the image's Rust tag is only a
# bootstrap — we compile with what upstream compiles with, by construction.
log "compiling claude-view-server (musl static)"
mkdir -p "$CARGO_DIR"
docker run --rm \
    -v "$SRC:/src" \
    -v "$CARGO_DIR:/cargo" \
    -w /src \
    -u "$(id -u):$(id -g)" \
    -e CARGO_HOME=/cargo \
    "$IMAGE" \
    cargo build --profile dist -p claude-view-server \
        --no-default-features --features dist

BIN="$SRC/target/dist/claude-view"
[ -f "$BIN" ] || { echo "!! build produced no binary at $BIN" >&2; exit 1; }

# --- 4. official frontend assets -------------------------------------------
log "official release assets v$VERSION"
mkdir -p "$OUT"
if [ ! -f "$OUT/dist/index.html" ]; then
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    gh release download "v$VERSION" --repo tombelieber/claude-view \
        -p "$TARBALL" -p checksums.txt -D "$tmp"
    ( cd "$tmp" && grep "$TARBALL" checksums.txt | sha256sum -c - )
    tar xzf "$tmp/$TARBALL" -C "$tmp" ./dist ./sidecar
    rm -rf "$OUT/dist" "$OUT/sidecar"
    mv "$tmp/dist" "$tmp/sidecar" "$OUT/"
    cp "$tmp/checksums.txt" "$OUT/checksums.txt"
else
    echo "frontend already staged; skipping download"
fi

# --- 5. stage the binary ---------------------------------------------------
log "staging"
install -m 0755 "$BIN" "$OUT/claude-view"
sha256sum "$OUT/claude-view" | awk '{print $1}' > "$OUT/claude-view.sha256"
cat > "$OUT/BUILD_INFO" <<EOF
version      = $VERSION
source_repo  = $REPO
source_tag   = v$VERSION
source_sha   = $HEAD_SHA
patches      = ${PATCHES[*]:-none}
patched_files= ${PATCHED_FILES:-none}
builder      = $IMAGE
target       = x86_64-unknown-linux-musl (static)
binary_sha256= $(cat "$OUT/claude-view.sha256")
frontend     = official release tarball ($TARBALL)
EOF

log "verifying"
file "$OUT/claude-view" | sed 's/^/  /'
echo "  linkage: $(ldd "$OUT/claude-view" 2>&1 | head -1)"
echo "  version: $("$OUT/claude-view" --version)"
echo
cat "$OUT/BUILD_INFO"

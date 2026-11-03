#!/usr/bin/env bash
# fetch-release.sh — pull this host's pinned tether binaries from the release.
#
# tether's binaries are built and published by the phy0x1a79ed/tether repository,
# not here. This host runs the version RELEASE pins, and this script is how it
# gets there: it reads RELEASE, compares the tag with the VERSION stamp beside
# the binaries, and when they differ fetches this host's role from the GitHub
# release, verifies every file against the release's SHA256SUMS, and swaps the
# binaries in. When they match it does nothing.
#
# # Why a script a deploy runs, not a hand-run step
#
# A deploy that changes RELEASE restarts the service, and run.sh calls this
# before the adapter registers — so the version a node runs follows the pin with
# no hand-run install. It runs before the control-WS lease, so a slow or failed
# fetch here cannot trip the ready-ASAP reaper.
#
# # Why a failed fetch is not fatal
#
# A GitHub outage must not take the relay down. A fetch that cannot complete
# leaves the binaries already in place untouched and leaves the VERSION stamp
# behind the pin, which `awm tether status` reports as a version lag. The node
# keeps serving the version it has.
#
# # Which files each role takes
#
# The operator's node takes the operator daemon. The public host (relay) takes
# the relay binary, every client, and both launchers — the launchers and clients
# into AWM_TETHER_ASSETS, which is the one directory the relay serves them from
# and the same variable the relay reads, so the two can never disagree.
#
# AWM_TETHER_BIN set means a developer is pointing the service at their own
# build; this then fetches nothing.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

# A developer's local build: fetch nothing, say so, and leave it to them.
if [ -n "${AWM_TETHER_BIN:-}" ]; then
    echo "tether fetch: AWM_TETHER_BIN=$AWM_TETHER_BIN set; using a local build, fetching nothing" >&2
    exit 0
fi

# The workspace root, by the same rule awm.config uses: AWM_WORKSPACE when set,
# else the first ancestor holding an AGENTS.md. Resolved the same way the
# adapter resolves paths.STATE_DIR, so the directory this writes to and the
# directory the adapter reads are always the one directory.
workspace_root() {
    if [ -n "${AWM_WORKSPACE:-}" ]; then
        printf '%s\n' "$AWM_WORKSPACE"
        return
    fi
    local d="$HERE"
    while [ "$d" != / ]; do
        [ -f "$d/AGENTS.md" ] && { printf '%s\n' "$d"; return; }
        d="$(dirname "$d")"
    done
    printf '%s\n' "$PWD"
}

STATE_DIR="$(workspace_root)/.awm/services/tether"
BIN_DIR="$STATE_DIR/bin"
VERSION_FILE="$BIN_DIR/VERSION"
FETCH_LOG="$STATE_DIR/fetch.log"

log() {
    mkdir -p "$STATE_DIR" 2>/dev/null || true
    printf '%s tether fetch: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$FETCH_LOG" >&2
}

ROLE="$(printf '%s' "${AWM_TETHER_ROLE:-operator}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
TAG="$(tr -d '[:space:]' < "$HERE/RELEASE")"
[ -n "$TAG" ] || { log "RELEASE is empty; nothing to fetch"; exit 1; }

GITHUB_BASE="${TETHER_GITHUB_BASE:-https://github.com/${TETHER_REPO:-phy0x1a79ed/tether}/releases/download}"

have=""
[ -f "$VERSION_FILE" ] && have="$(tr -d '[:space:]' < "$VERSION_FILE")"
if [ "$have" = "$TAG" ]; then
    exit 0
fi
log "pinned $TAG, installed ${have:-none}; fetching role $ROLE"

# One asset, from the release. TETHER_RELEASE_DIR copies from a local directory
# instead of downloading, for a test that has no published release to reach.
get() {
    local asset="$1" out="$2"
    if [ -n "${TETHER_RELEASE_DIR:-}" ]; then
        cp "$TETHER_RELEASE_DIR/$asset" "$out" 2>/dev/null
    elif command -v curl >/dev/null 2>&1; then
        curl -fsSL --max-time 180 "$GITHUB_BASE/$TAG/$asset" -o "$out" 2>/dev/null
    elif command -v wget >/dev/null 2>&1; then
        wget -q -T 180 -O "$out" "$GITHUB_BASE/$TAG/$asset" 2>/dev/null
    else
        log "no curl or wget on this host; cannot fetch"
        return 1
    fi
}

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    elif command -v openssl >/dev/null 2>&1; then
        openssl dgst -sha256 "$1" | awk '{print $NF}'
    else
        return 1
    fi
}

stage="$(mktemp -d "${TMPDIR:-/tmp}/tether-fetch.XXXXXX")"
trap 'rm -rf "$stage"' EXIT

# SHA256SUMS first: it names every published asset and is what each binary is
# checked against. No sums, no fetch.
get SHA256SUMS "$stage/SHA256SUMS" \
    || { log "could not fetch SHA256SUMS for $TAG; leaving the binaries as they are"; exit 1; }

# What this role takes, and where each file lands. The relay binary and the
# operator daemon are renamed to the plain names the daemon supervises
# (paths.RELAY_BIN / paths.OPERATOR_BIN); the clients and launchers keep the
# names the launcher asks the relay for.
declare -a ASSET DEST
add() { ASSET+=("$1"); DEST+=("$2"); }

case "$ROLE" in
    relay)
        add tether-relay-linux-x86_64 "$BIN_DIR/tether-relay"
        if [ -n "${AWM_TETHER_ASSETS:-}" ]; then
            add tether "$AWM_TETHER_ASSETS/tether"
            add tether.ps1 "$AWM_TETHER_ASSETS/tether.ps1"
            while read -r _sum name; do
                [ -n "${name:-}" ] || continue
                case "$name" in
                    SHA256SUMS|tether|tether.ps1|tether-relay-*|tether-operator-*) ;;
                    tether-*) add "$name" "$AWM_TETHER_ASSETS/bin/$name" ;;
                esac
            done < "$stage/SHA256SUMS"
        else
            log "AWM_TETHER_ASSETS unset; fetching the relay binary only (the relay serves no clients until it is set)"
        fi
        ;;
    *)
        add tether-operator-linux-x86_64 "$BIN_DIR/tether-operator"
        ;;
esac

# Phase 1: fetch and verify everything into the stage. Nothing is installed yet,
# so any failure here leaves the live binaries exactly as they were.
for i in "${!ASSET[@]}"; do
    a="${ASSET[$i]}"
    want="$(awk -v n="$a" '$2 == n { print $1; exit }' "$stage/SHA256SUMS")"
    [ -n "$want" ] || { log "$a is not listed in SHA256SUMS for $TAG; aborting"; exit 1; }
    get "$a" "$stage/$a" || { log "could not fetch $a for $TAG; aborting, binaries unchanged"; exit 1; }
    got="$(sha256_of "$stage/$a")" \
        || { log "no sha256 tool (sha256sum, shasum or openssl); cannot verify, aborting"; exit 1; }
    if [ "$got" != "$want" ]; then
        log "$a does not match SHA256SUMS (want $want, got $got); aborting, binaries unchanged"
        exit 1
    fi
done

# Phase 2: every file is present and verified — move them into place, then stamp
# the version last so a crash between moves never claims a version it does not
# fully have.
for i in "${!ASSET[@]}"; do
    a="${ASSET[$i]}"; d="${DEST[$i]}"
    mkdir -p "$(dirname "$d")"
    chmod 0755 "$stage/$a"
    mv -f "$stage/$a" "$d"
done

mkdir -p "$BIN_DIR"
printf '%s\n' "$TAG" > "$VERSION_FILE"
log "installed $TAG for role $ROLE"

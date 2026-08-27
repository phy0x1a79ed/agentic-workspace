#!/usr/bin/env bash
# Rebuild + restart only the Penpot production image(s) whose source has
# actually changed since their own last build, against the LOCAL stack
# (`stack.py`'s `penpot-local` compose project). Mirrors scripts/promote.sh's
# shape: gated stages, idempotent, sha-checked at each hop, refuses to finish
# unless every hop agrees. A sirius variant is a natural future extension of
# this same shape, not built here (T15's scope is local only).
#
#   awm/services/penpot/scripts/promote-local.sh [module ...]
#
# With no arguments: frontend, backend, exporter (mcp/storybook are unused by
# this deployment — see docker-compose.local.yml). Each module's source is
# `<fork>/<module>/` plus `<fork>/common/`, the Clojure(Script) lib every one
# of the three depends on (`:local/root "../common"` in each module's
# deps.edn) — a change under common/ therefore always counts as a change for
# every module.
#
# Why not just trust Docker's layer cache: `docker build` would happily
# no-op an unchanged bundle-<module>/ tree, but getting to that point still
# means running manage.sh's build-<module>-bundle first — a fresh one-shot
# devenv container compiling frontend/backend/exporter from source, minutes
# even when nothing changed. The sha-diff below skips *that*, not just the
# final `docker build`.
#
# State: docker/images/.promote-sha-<module>.local remembers the fork HEAD
# sha this script last built that module from. It matches the
# `/docker/images/*.local` .gitignore pattern already in the fork, so this
# never needs a .gitignore edit. Only ever written after a build from a CLEAN
# working tree — see `module_changed` below for why a dirty tree skips it.

set -euo pipefail

WORKSPACE_ROOT="${AWM_WORKSPACE:-/home/tony/agentic_workspace}"
FORK_DIR="${PENPOT_FORK_DIR:-$WORKSPACE_ROOT/projects/penpot/dev}"
COMPOSE_DIR="${PENPOT_COMPOSE_DIR:-$FORK_DIR/docker/images}"
COMPOSE_PROJECT="${PENPOT_COMPOSE_PROJECT:-penpot-local}"
COMPOSE_FILE="${PENPOT_COMPOSE_FILE:-docker-compose.yaml}"
IFS=',' read -r -a OVERRIDE_FILES <<< "${PENPOT_COMPOSE_OVERRIDE_FILES:-docker-compose.local.yml}"
ENV_FILE="${PENPOT_COMPOSE_ENV_FILE:-.env.local}"
IMAGE_NAMESPACE="${PENPOT_IMAGE_NAMESPACE:-penpotapp}"
PENPOT_URL="${PENPOT_LOCAL_URL:-http://127.0.0.1:9001}"
HEALTH_TIMEOUT_S="${PENPOT_HEALTH_TIMEOUT_S:-180}"
#: Consecutive passing checks required before the stack counts as healthy —
#: a container that answers once and then crash-loops must not pass on a
#: single lucky curl. `docker compose up -d` itself exits 0 for exactly that
#: container, which is the whole reason this loop exists.
HEALTH_STREAK_NEEDED=3
HEALTH_INTERVAL_S=3

declare -A MOD_TO_SERVICE=([frontend]=penpot-frontend [backend]=penpot-backend [exporter]=penpot-exporter)

MODULES=("$@")
[ ${#MODULES[@]} -eq 0 ] && MODULES=(frontend backend exporter)
for m in "${MODULES[@]}"; do
    [ -n "${MOD_TO_SERVICE[$m]:-}" ] || { echo "unknown module '$m' (want: frontend backend exporter)" >&2; exit 1; }
done

step() { echo; echo "== $*"; }
sha() { git -C "$FORK_DIR" rev-parse --short "${1:-HEAD}"; }

compose() {
    local args=(docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE")
    local f
    for f in "${OVERRIDE_FILES[@]}"; do args+=(-f "$f"); done
    [ -n "$ENV_FILE" ] && args+=(--env-file "$ENV_FILE")
    ( cd "$COMPOSE_DIR" && "${args[@]}" "$@" )
}

# `docker compose ps --format json` prints either NDJSON or a single JSON
# array depending on the compose version — normalize both to NDJSON so every
# caller below can just `jq -c 'select(...)'` it.
ps_json() {
    local out
    out=$(compose ps --all --format json 2>/dev/null) || { echo ""; return 0; }
    [ -z "$out" ] && { echo ""; return 0; }
    if echo "$out" | jq -e 'type=="array"' >/dev/null 2>&1; then
        echo "$out" | jq -c '.[]'
    else
        echo "$out"
    fi
}

# Mirrors stack.py's `_container_up`: a declared healthcheck is authoritative
# when present (healthy only); without one, State==running is all there is.
service_up() {
    local svc="$1" line state health
    line=$(ps_json | jq -c "select(.Service==\"$svc\")" | tail -n1)
    [ -n "$line" ] || return 1
    state=$(echo "$line" | jq -r '.State // ""' | tr '[:upper:]' '[:lower:]')
    health=$(echo "$line" | jq -r '.Health // ""' | tr '[:upper:]' '[:lower:]')
    if [ -n "$health" ]; then [ "$health" = healthy ]; else [ "$state" = running ]; fi
}

running_image() {
    local svc="$1"
    ps_json | jq -r "select(.Service==\"$svc\") | .Image" | tail -n1
}

[ -d "$FORK_DIR/.git" ] || [ -f "$FORK_DIR/.git" ] || { echo "no fork checkout at $FORK_DIR" >&2; exit 1; }
[ -f "$COMPOSE_DIR/$COMPOSE_FILE" ] || { echo "no compose file at $COMPOSE_DIR/$COMPOSE_FILE" >&2; exit 1; }
[ -x "$FORK_DIR/manage.sh" ] || { echo "no manage.sh at $FORK_DIR" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker not on PATH" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq not on PATH (needed to read docker compose ps --format json)" >&2; exit 1; }

step "1. sha"
HEAD_SHA=$(sha HEAD)
echo "   fork HEAD @ $HEAD_SHA"

module_state_file() { echo "$COMPOSE_DIR/.promote-sha-$1.local"; }

# A module needs rebuilding when: it has never been built (no state file),
# its last-built sha is no longer resolvable (e.g. history was rewritten), a
# committed change touched <module>/ or common/ since that sha, or the
# working tree is dirty under either path right now. Dirty always means
# "rebuild" — there is no sha that describes uncommitted content, so nothing
# is recorded for it either; the next CLEAN run is what advances the state
# file, which is deliberate: it means a dirty rebuild never falsely marks a
# module as caught up.
module_changed() {
    local mod="$1" state_file
    state_file=$(module_state_file "$mod")
    if [ -n "$(git -C "$FORK_DIR" status --porcelain -- "$mod" common)" ]; then
        echo "dirty"; return
    fi
    if [ ! -f "$state_file" ]; then
        echo "never-built"; return
    fi
    local last; last=$(cat "$state_file")
    if ! git -C "$FORK_DIR" cat-file -e "${last}^{commit}" 2>/dev/null; then
        echo "unknown-last-sha"; return
    fi
    if [ -n "$(git -C "$FORK_DIR" diff --name-only "$last" HEAD -- "$mod" common)" ]; then
        echo "changed"; return
    fi
    echo "unchanged"
}

step "2. what changed"
declare -A REASON
TO_BUILD=()
for mod in "${MODULES[@]}"; do
    reason=$(module_changed "$mod")
    REASON[$mod]=$reason
    [ "$reason" = unchanged ] || TO_BUILD+=("$mod")
    printf '   %-9s %s\n' "$mod" "$reason"
done

if [ ${#TO_BUILD[@]} -eq 0 ]; then
    echo
    # "Nothing to build" is a claim about git, not about the box. The state
    # files can agree with HEAD while the running container is something else
    # entirely: a dirty build that was deployed and then reverted, a manual
    # `docker compose up`, a hand-edited overlay. Exiting 0 here without
    # looking would report success for a stack running unknown images, so the
    # running image is checked even on the path that builds nothing.
    step "2b. verify what is actually running"
    DRIFT=0
    for mod in "${MODULES[@]}"; do
        svc="${MOD_TO_SERVICE[$mod]}"
        want="${IMAGE_NAMESPACE}/${mod}:$(cat "$(module_state_file "$mod")" 2>/dev/null || echo '?')"
        got=$(running_image "$svc")
        if [ -z "$got" ]; then
            printf '   %-16s not running\n' "$svc"
        elif [ "$got" != "$want" ]; then
            printf '   %-16s DRIFT expected=%-34s running=%s\n' "$svc" "$want" "$got"
            DRIFT=1
        else
            printf '   %-16s ok %s\n' "$svc" "$got"
        fi
    done
    if [ "$DRIFT" -ne 0 ]; then
        echo
        echo "nothing needed rebuilding, but a running image is not the one recorded as built." >&2
        echo "re-run with FORCE=1, or bring the stack up from the overlay, before trusting this stack." >&2
        exit 1
    fi
    echo
    echo "nothing changed since the last build of any of: ${MODULES[*]}"
    exit 0
fi

declare -A TAGS
for mod in "${TO_BUILD[@]}"; do
    step "3. build $mod"
    tag="$HEAD_SHA"
    [ "${REASON[$mod]}" = dirty ] && tag="${HEAD_SHA}-dirty"
    TAGS[$mod]="$tag"
    echo "   compiling ($mod -> \"$FORK_DIR/manage.sh build-${mod}-bundle\", inside a one-shot devenv container)"

    ( cd "$FORK_DIR" && ./manage.sh "build-${mod}-bundle" )

    # Same rsync manage.sh's own _build-release-docker-image uses to project
    # the fresh bundle into docker/images/ — but the docker build step is
    # ours, not manage.sh's build-<mod>-docker-image: that hardcodes
    # Dockerfile.<mod> (the dhi.io-based real one, 401s with no registry
    # credentials on this host) and tags penpotapp/<mod>:$CURRENT_BRANCH,
    # neither of which this local deployment can use.
    rsync -a --delete "$FORK_DIR/bundles/$mod/" "$COMPOSE_DIR/bundle-$mod/"

    ( cd "$COMPOSE_DIR" && docker build \
        -t "$IMAGE_NAMESPACE/$mod:$tag" \
        --build-arg BUNDLE_PATH="./bundle-$mod/" \
        -f "Dockerfile.$mod.local" . )

    if [ "$tag" = "$HEAD_SHA" ]; then
        echo "$HEAD_SHA" > "$(module_state_file "$mod")"
    else
        echo "   working tree dirty under $mod/ or common/ — not recording a last-built sha"
    fi
    echo "   built $IMAGE_NAMESPACE/$mod:$tag"
done

step "4. rewrite compose overlay image tags"
# Touch only the `image:` line for each rebuilt module's own service — the
# override also carries JVM heap caps, nginx worker counts and postgres
# shared_buffers (see docker-compose.local.yml's T4.5 sizing) that a
# less-targeted rewrite (e.g. a whole-service block replace) could clobber.
for mod in "${TO_BUILD[@]}"; do
    tag="${TAGS[$mod]}"
    for f in "${OVERRIDE_FILES[@]}"; do
        path="$COMPOSE_DIR/$f"
        [ -f "$path" ] || continue
        if grep -q "image: \"${IMAGE_NAMESPACE}/${mod}:" "$path"; then
            sed -i -E "s#(image: \"${IMAGE_NAMESPACE}/${mod}:)[^\"]+(\")#\1${tag}\2#" "$path"
            echo "   $f: $mod -> $tag"
        fi
    done
done

step "5. restart changed service(s)"
for mod in "${TO_BUILD[@]}"; do
    svc="${MOD_TO_SERVICE[$mod]}"
    echo "   docker compose up -d --no-deps $svc"
    compose up -d --no-deps "$svc"
done

step "6. health check"
# A zero exit from `up -d` above proves nothing: a container that starts and
# immediately crash-loops exits 0 there too (compose's job is "did I ask
# dockerd to start it", not "did it stay up"). So health is judged only by
# HEALTH_STREAK_NEEDED consecutive passes of both signals below, spaced
# HEALTH_INTERVAL_S apart, within HEALTH_TIMEOUT_S — one lucky response
# right after a restart, followed by a death, must not read as healthy.
health_once() {
    curl -fsS -m 5 -o /dev/null "$PENPOT_URL/readyz" || return 1   # backend, proxied by frontend nginx
    curl -fsS -m 5 -o /dev/null "$PENPOT_URL/" || return 1          # frontend itself
    local mod
    for mod in "${TO_BUILD[@]}"; do
        service_up "${MOD_TO_SERVICE[$mod]}" || return 1
    done
}

streak=0
deadline=$((SECONDS + HEALTH_TIMEOUT_S))
ok=0
while [ "$SECONDS" -lt "$deadline" ]; do
    if health_once; then
        streak=$((streak + 1))
        echo "   pass $streak/$HEALTH_STREAK_NEEDED"
        if [ "$streak" -ge "$HEALTH_STREAK_NEEDED" ]; then
            ok=1
            break
        fi
    else
        [ "$streak" -gt 0 ] && echo "   streak broken, retrying"
        streak=0
    fi
    sleep "$HEALTH_INTERVAL_S"
done

if [ "$ok" -ne 1 ]; then
    echo "penpot did not report healthy within ${HEALTH_TIMEOUT_S}s of restart" >&2
    exit 1
fi

step "7. verify"
FAIL=0
for mod in "${TO_BUILD[@]}"; do
    svc="${MOD_TO_SERVICE[$mod]}"
    want="${IMAGE_NAMESPACE}/${mod}:${TAGS[$mod]}"
    got=$(running_image "$svc")
    printf '   %-16s built=%-40s running=%s\n' "$svc" "$want" "$got"
    [ "$got" = "$want" ] || FAIL=1
done
[ "$FAIL" -eq 0 ] || { echo "a running container's image does not match what this run just built" >&2; exit 1; }

echo
echo "promoted ${TO_BUILD[*]} @ $HEAD_SHA — restarted and healthy"

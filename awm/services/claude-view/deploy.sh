#!/usr/bin/env bash
# deploy.sh — ship claude-view from this worktree to the production gateway.
#
# `awm deploy` already owns the general case: it reinstalls when the dist set
# changed, restarts the systemd gateway, reaps orphaned adapters, and verifies
# every enabled service came back. This script is only the parts of a
# claude-view deploy that are specific to this service, wrapped around it:
#
#   1. the commits have to be CHERRY-PICKED, not merged. dev and release carry
#      the same work under different SHAs (dev +104 / release +54 at the time of
#      writing), so `merge dev -> release` would drag a hundred unrelated
#      commits along with these five.
#
#   2. `vendor/` is gitignored — the 33 MB binary is a build artifact, not
#      source. It has to be staged into the release tree separately, and it has
#      to happen BEFORE `awm deploy`, because the install pass runs each
#      service's install.sh and claude-view's builds the binary when it is not
#      already staged. Miss the ordering and a deploy turns into a ten-minute
#      Docker build.
#
#   3. "the service came back" is a weaker claim here than for a pure-Python
#      service. The adapter registers happily while the thing it supervises is
#      dead, missing its node, or writing hooks nobody reads — by design, so
#      that `status` can report why. So verification reads `status` rather than
#      the registration.
#
# ROLLBACK IS A REVERT, NEVER A RESET. The release worktree legitimately carries
# unrelated uncommitted work (it is a live worktree people hotfix in), and
# `reset --hard` would silently destroy it. `--rollback` reverts exactly the
# commits this script applied and leaves everything else alone. The instant
# escape hatch that needs no git at all is `awm services disable claude-view`.
#
# Usage:
#   ./deploy.sh --dry-run     # print the plan, change nothing
#   ./deploy.sh               # deploy
#   ./deploy.sh --rollback    # revert the last deploy's commits and re-deploy
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(git -C "$HERE" rev-parse --show-toplevel)"
BRANCH="$(git -C "$SRC" rev-parse --abbrev-ref HEAD)"
VERSION="${CLAUDE_VIEW_VERSION:-0.45.0}"
STATE="$HERE/state"
RECORD="$STATE/last-deploy.json"

#: The branch these commits are cut against. Everything in BASE..BRANCH is
#: "this scope's work" and nothing else is.
BASE="${CLAUDE_VIEW_DEPLOY_BASE:-dev}"

DRY=0; ROLLBACK=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY=1 ;;
        --rollback) ROLLBACK=1 ;;
        *) echo "unknown flag: $arg" >&2; exit 2 ;;
    esac
done

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf '\033[31mFATAL: %s\033[0m\n' "$*" >&2; exit 1; }

# `awm` is on PATH for an interactive shell but not necessarily for a script.
awm() {
    if command -v awm >/dev/null 2>&1; then command awm "$@"
    else mamba run -n "${AWM_ENV:-awm}" --no-capture-output awm "$@"; fi
}

# -- resolve the release worktree -------------------------------------------
# No `exit` in the awk and no `head` after it: either closes the pipe while git
# is still writing, and under `pipefail` that SIGPIPE is a fatal error with no
# output at all. Take every match and keep the first in the shell instead.
RELEASE="$(git -C "$SRC" worktree list --porcelain \
    | awk '/^worktree /{w=$2} /^branch refs\/heads\/release$/{print w}')"
RELEASE="${RELEASE%%$'\n'*}"
[ -n "$RELEASE" ] || die "no worktree checked out on 'release'"

# -- rollback ----------------------------------------------------------------
if [ "$ROLLBACK" = 1 ]; then
    [ -f "$RECORD" ] || die "no $RECORD — nothing recorded to roll back"
    SHAS="$(python3 -c "
import json; print(' '.join(reversed(json.load(open('$RECORD'))['release_shas'])))")"
    [ -n "$SHAS" ] || die "record contains no commits"
    say "Rolling back $(echo "$SHAS" | wc -w) commit(s) from release"
    info "$SHAS"
    [ "$DRY" = 1 ] && { info "(dry run — stopping here)"; exit 0; }
    # --no-commit across all of them, then one revert commit: a partially
    # applied rollback is worse than none.
    git -C "$RELEASE" revert --no-commit $SHAS \
        || { git -C "$RELEASE" revert --abort 2>/dev/null || true
             die "revert conflicted — resolve by hand in $RELEASE"; }
    git -C "$RELEASE" commit -q -m "Revert claude-view deploy ($(date -I))"
    ( cd "$RELEASE" && awm deploy )
    say "Rolled back. The service folder is gone from release, so the gateway"
    info "no longer discovers it. Nothing else was touched."
    exit 0
fi

# -- preflight ---------------------------------------------------------------
say "Preflight"

[ -z "$(git -C "$SRC" status --porcelain)" ] \
    || die "$SRC has uncommitted changes — commit them first"
info "source worktree clean ($BRANCH)"

git -C "$SRC" rev-parse --verify -q "$BASE" >/dev/null \
    || die "base branch '$BASE' not found"

# Release is a live worktree and may legitimately carry unrelated in-flight
# edits, so this is deliberately NOT a blanket clean check — only a conflict
# check against the paths this deploy actually writes.
PATHS="awm/services/claude-view awm/services/httpsfront"
DIRTY="$(git -C "$RELEASE" status --porcelain -- $PATHS)"
[ -z "$DIRTY" ] || die "release has uncommitted changes to paths we deploy:
$DIRTY"
OTHER="$(git -C "$RELEASE" status --porcelain | wc -l)"
info "release clean on our paths ($OTHER unrelated uncommitted file(s) — left alone)"

[ -x "$HERE/vendor/v$VERSION/claude-view" ] \
    || die "no staged binary at vendor/v$VERSION/claude-view — run ./install.sh"
info "binary staged: v$VERSION"

# -- plan --------------------------------------------------------------------
say "Plan"
mapfile -t COMMITS < <(git -C "$SRC" rev-list --reverse "$BASE..$BRANCH")
[ "${#COMMITS[@]}" -gt 0 ] || die "no commits in $BASE..$BRANCH to deploy"
for c in "${COMMITS[@]}"; do
    info "$(git -C "$SRC" log -1 --format='%h %s' "$c")"
done
info "-> cherry-pick ${#COMMITS[@]} commit(s) onto release, stage v$VERSION, awm deploy"

if [ "$DRY" = 1 ]; then
    say "Dry run — nothing changed"
    ( cd "$RELEASE" && awm deploy --dry-run ) || true
    exit 0
fi

# -- trial -------------------------------------------------------------------
# Cherry-pick into a throwaway detached worktree first. A conflict discovered
# here costs nothing; the same conflict discovered in the release worktree
# leaves it mid-cherry-pick with a live gateway running out of it.
say "Trial cherry-pick"
TRIAL="$(mktemp -d)/trial"
cleanup_trial() { git -C "$SRC" worktree remove --force "$TRIAL" 2>/dev/null || true
                  git -C "$SRC" worktree prune 2>/dev/null || true; }
trap cleanup_trial EXIT
git -C "$SRC" worktree add -d "$TRIAL" release -q
if ! git -C "$TRIAL" cherry-pick "$BASE..$BRANCH" >/dev/null 2>&1; then
    git -C "$TRIAL" cherry-pick --abort 2>/dev/null || true
    die "cherry-pick conflicts against release — resolve before deploying"
fi
info "applies cleanly"
cleanup_trial; trap - EXIT

# -- apply -------------------------------------------------------------------
say "Cherry-picking onto release"
PREV="$(git -C "$RELEASE" rev-parse HEAD)"
if ! git -C "$RELEASE" cherry-pick "$BASE..$BRANCH"; then
    git -C "$RELEASE" cherry-pick --abort 2>/dev/null || true
    die "cherry-pick failed in $RELEASE (was clean in trial — investigate)"
fi
mapfile -t NEW < <(git -C "$RELEASE" rev-list --reverse "$PREV..HEAD")
info "applied ${#NEW[@]} commit(s); release $PREV -> $(git -C "$RELEASE" rev-parse --short HEAD)"

# -- stage the binary --------------------------------------------------------
# Gitignored, so the cherry-pick did not carry it. Same host, same file — a
# copy, not a rebuild. BUILD_INFO travels with it so the release tree records
# the provenance of what it is actually running.
say "Staging binary + frontend into release"
DEST="$RELEASE/awm/services/claude-view/vendor/v$VERSION"
mkdir -p "$DEST"
rsync -a --delete "$HERE/vendor/v$VERSION/" "$DEST/"
info "$(du -sh "$DEST" | cut -f1) -> $DEST"

# -- install from the release tree ------------------------------------------
# Not left to `awm deploy`'s change detection, because the thing most likely to
# be missing is invisible to it: `.runtime-env` is gitignored, so the
# cherry-pick cannot carry it, and without it run.sh execs a bare `python` on
# systemd's minimal PATH and the service never starts. install.sh writes it.
#
# This is the one context where running install.sh is correct rather than
# dangerous: its httpsfront `pip install -e` re-points the live :12100 front at
# whatever tree it runs from, and here that tree IS release. Never run it from a
# feature worktree.
say "Installing from release"
bash "$RELEASE/awm/services/claude-view/install.sh" >/dev/null \
    || die "install.sh failed in $RELEASE"
[ -f "$RELEASE/awm/services/claude-view/.runtime-env" ] \
    || die "install.sh left no .runtime-env — run.sh would exec a bare python"
info "$(sed -n 's/^AWM_PYTHON=//p' "$RELEASE/awm/services/claude-view/.runtime-env")"

# -- deploy ------------------------------------------------------------------
say "awm deploy"
( cd "$RELEASE" && awm deploy ) || die "awm deploy failed — see above.
Roll back with: $0 --rollback"

# -- verify ------------------------------------------------------------------
# The adapter registering is not evidence the service works: it registers even
# when the binary is missing, node is absent, or the hooks went to a scratch
# file. Read what status actually reports.
say "Verify"
sleep 3
python3 - "$RECORD" "$PREV" "${NEW[@]}" <<'PY'
import json, subprocess, sys, time, urllib.request

record, prev, *shas = sys.argv[1:]

def status(timeout=90):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:7819/svc/claude-view/fn/status",
                data=b"{}", headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except Exception as exc:
            last = exc
            time.sleep(3)
    raise SystemExit(f"FATAL: claude-view never answered status: {last}")

s = status()
proc, health = s.get("process", {}), s.get("health", {})
hooks, front = proc.get("hooks", {}), s.get("front", {})

checks = [
    ("child running",        proc.get("running") is True,          proc.get("last_error")),
    ("upstream health ok",   health.get("ok") is True,             health.get("error")),
    ("binary installed",     proc.get("installed") is True,        proc.get("binary")),
    ("sidecar capable",      proc.get("sidecar_capable") is True,  "no node on PATH -> chat dies silently"),
    ("hooks fleet-wide",     hooks.get("fleet_wide") is True,      hooks.get("settings")),
    ("hooks complete",       hooks.get("complete") is True,        f"{hooks.get('ours')} entries"),
    ("front serving",        front.get("serving") is True,         front.get("error")),
    ("front TLS",            front.get("tls") is True,             None),
]
bad = 0
for name, ok, detail in checks:
    print(f"   {'PASS' if ok else 'FAIL'}  {name}" + (f"   ({detail})" if not ok and detail else ""))
    bad += not ok

print(f"\n   version={s.get('version')} pid={proc.get('pid')} "
      f"upstream=:{proc.get('upstream_port')} sidecar=:{proc.get('sidecar_port')} "
      f"front=:{front.get('listener_port')} index={s.get('index',{}).get('db_bytes',0)//1048576}MB")

json.dump({"release_shas": shas, "release_prev": prev,
           "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}, open(record, "w"), indent=2)

if bad:
    raise SystemExit(f"\nFATAL: {bad} check(s) failed. Roll back with: deploy.sh --rollback")
PY

say "Deployed"
info "dashboard: https://<this-host>:12110/  (awm edge session; sign in at :12100 first)"
info "rollback:  $0 --rollback     instant stop: awm services disable claude-view"

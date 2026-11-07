#!/usr/bin/env bash
# Checks the core and core plugin pins (scripts/pins.sh, release-plugins.sh, awm-deploy.sh) on
# a throwaway checkout with a fake cargo build and a fake awm binary. Needs git, no container.
#   test/pins/plugins.sh
set -euo pipefail
repo="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
node="$tmp/node"
fails=0

check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; fails=$((fails + 1)); fi; }
commit() { git -C "$node" add -A && git -C "$node" -c user.name=t -c user.email=t@t commit -qm "$1"; }
v() { (cd "$node" && . scripts/pins.sh && "$@" .); }
pv() { (cd "$node" && . scripts/pins.sh && plugin_version . "$1"); }
deploy() { AWM_DIR="$node/.awm" bash "$node/scripts/awm-deploy.sh" "$@"; }
release() { AWM_DIR="$node/.awm" bash "$node/scripts/release-plugins.sh" "$@"; }

mkdir -p "$node/scripts" "$node/core/crates/awm-proto" "$node/core/crates/awm-base" "$node/core/crates/awm-gateway" \
    "$node/core/crates/awm-cli" "$node/core/crates/awm-sdk" \
    "$node/awm/services/svc/pages/ui" "$node/.awm/artifacts"
cp "$repo"/scripts/{pins.sh,awm-deploy.sh,release-plugins.sh} "$node/scripts/"
cat > "$node/scripts/rust-build.sh" <<'EOF'
#!/usr/bin/env bash
# Fake cargo: each -p awm-plugin-<name> leaves a binary that prints its name and source.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
out="${AWM_CARGO_TARGET_DIR:-$here/core/target}/x86_64-unknown-linux-musl/release"
mkdir -p "$out"
while [ $# -gt 0 ]; do
    if [ "$1" = -p ]; then n="${2#awm-plugin-}"; printf '#!/bin/sh\necho %s %s\n' "$n" "$(cat "$here/core/plugins/$n/src/main.rs")" > "$out/$n"; shift; fi
    shift
done
echo "$*" >> "$here/.awm/builds"
EOF
chmod +x "$node/scripts/rust-build.sh"
printf '[workspace]\nmembers = ["crates/*", "plugins/*"]\n\n[workspace.dependencies]\nserde = "1"\nawm-proto = { path = "crates/awm-proto" }\nawm-gateway = { path = "crates/awm-gateway" }\nawm-sdk = { path = "crates/awm-sdk" }\n' > "$node/core/Cargo.toml"
printf 'lock one\n' > "$node/core/Cargo.lock"
printf 'FROM rust\n' > "$node/core/Dockerfile"
printf '[package]\nname = "awm-proto"\n\n[dependencies]\nawm-base = { path = "../awm-base" }\nserde.workspace = true\n\n[dev-dependencies]\nawm-gateway.workspace = true\n' > "$node/core/crates/awm-proto/Cargo.toml"
printf '[package]\nname = "awm-base"\n' > "$node/core/crates/awm-base/Cargo.toml"
printf '[package]\nname = "awm-gateway"\n\n[dependencies]\nawm-proto.workspace = true\n' > "$node/core/crates/awm-gateway/Cargo.toml"
printf '[package]\nname = "awm-cli"\n\n[dependencies]\nawm-gateway.workspace = true\n' > "$node/core/crates/awm-cli/Cargo.toml"
printf '[package]\nname = "awm-sdk"\n\n[dependencies]\nawm-proto.workspace = true\n' > "$node/core/crates/awm-sdk/Cargo.toml"
for c in awm-proto awm-base awm-gateway awm-cli awm-sdk; do mkdir -p "$node/core/crates/$c/src"; echo one > "$node/core/crates/$c/src/lib.rs"; done
printf 'pins = ["scripts/core.pin", "scripts/pages.pin"]\n' > "$node/deploy.toml"
printf '/.awm/\n' > "$node/.gitignore"
echo src > "$node/awm/services/svc/pages/ui/index.html"
git init -q "$node"
commit zero

# Zero plugins.
check "no core/plugins lists no plugin" '[ -z "$(v core_plugins)" ]'
check "release-plugins with no plugin builds nothing" 'release >/dev/null && [ ! -e "$node/.awm/builds" ]'
core0="$(v core_version)"
check "core version format" '[[ $core0 =~ ^core-[0-9a-f]{12}$ ]]'
check "the core closure is awm-cli and its local crates" \
    '[ "$(v core_closure | tr "\n" " ")" = "core/crates/awm-base core/crates/awm-cli core/crates/awm-gateway core/crates/awm-proto " ]'
echo two > "$node/core/crates/awm-sdk/src/lib.rs"; commit "sdk two"
check "a crate outside awm-cli's closure leaves the core version" '[ "$(v core_version)" = "$core0" ]'
mkdir -p "$node/core/deploy"; echo unit > "$node/core/deploy/awm.service"; commit "a unit"
check "a core/ file outside the closure leaves the core version" '[ "$(v core_version)" = "$core0" ]'

# One plugin, depending on awm-sdk through the workspace, and awm-proto and awm-base through it.
mkdir -p "$node/core/plugins/hello/src"
printf '[package]\nname = "awm-plugin-hello"\n\n[[bin]]\nname = "hello"\n\n[dependencies]\nawm-sdk = { workspace = true }\n' > "$node/core/plugins/hello/Cargo.toml"
printf '[plugin]\nname = "hello"\n' > "$node/core/plugins/hello/awm-plugin.toml"
echo one > "$node/core/plugins/hello/src/main.rs"
commit hello
check "a new plugin leaves the core version" '[ "$(v core_version)" = "$core0" ]'
check "core_plugins lists hello" '[ "$(v core_plugins)" = hello ]'
check "the closure is the plugin and its local crates" \
    '[ "$(cd "$node" && . scripts/pins.sh && plugin_closure . hello | tr "\n" " ")" = "core/crates/awm-base core/crates/awm-proto core/crates/awm-sdk core/plugins/hello " ]'
hello0="$(pv hello)"
check "plugin version format" '[[ $hello0 =~ ^hello-[0-9a-f]{12}$ ]]'

echo two > "$node/core/plugins/hello/src/main.rs"; commit "hello two"
check "a plugin edit leaves the core version" '[ "$(v core_version)" = "$core0" ]'
check "a plugin edit moves its version" '[ "$(pv hello)" != "$hello0" ]'
hello1="$(pv hello)"
echo two > "$node/core/crates/awm-gateway/src/lib.rs"; commit "gateway two"
check "a crate outside the closure leaves the plugin version" '[ "$(pv hello)" = "$hello1" ]'
check "a core crate edit moves the core version" '[ "$(v core_version)" != "$core0" ]'
echo two > "$node/core/crates/awm-base/src/lib.rs"; commit "base two"
check "a transitive crate edit moves the plugin version" '[ "$(pv hello)" != "$hello1" ]'
hello2="$(pv hello)"
printf 'lock two\n' > "$node/core/Cargo.lock"; commit "lock two"
check "a Cargo.lock change moves the plugin version" '[ "$(pv hello)" != "$hello2" ]'
core1="$(v core_version)"
printf 'FROM rust:2\n' > "$node/core/Dockerfile"; commit "toolchain two"
check "a Dockerfile change moves the core version" '[ "$(v core_version)" != "$core1" ]'

# The pins: release-plugins refuses an unlisted pin, builds a stale one, and keeps a current one.
check "release-plugins refuses a pin deploy.toml does not list" '! release --dry-run 2>/dev/null'
printf 'pins = ["scripts/core.pin", "scripts/pages.pin", "scripts/plugins/hello.pin"]\n' > "$node/deploy.toml"; commit listed
check "a dry run names the stale plugin and builds nothing" \
    'release --dry-run | grep -q "hello: hello-.* is stale" && [ ! -e "$node/.awm/builds" ]'
release >/dev/null
check "release-plugins pins hello" 'grep -q "^version=$(pv hello)$" "$node/scripts/plugins/hello.pin"'
check "the binary is in the store" '[ -x "$node/.awm/artifacts/awm-plugin-$(pv hello)-linux-x86_64-musl" ]'
commit "deploy: refresh pins"
check "a second run finds the pin current" 'release | grep -q "is pinned" && [ "$(wc -l < "$node/.awm/builds")" = 1 ]'
echo three > "$node/core/plugins/hello/x"; commit "hello three"
echo dirty > "$node/core/crates/awm-proto/src/lib.rs"
check "release-plugins refuses an uncommitted closure" '! release >/dev/null 2>&1'
git -C "$node" checkout -q HEAD -- core/crates/awm-proto/src/lib.rs

# The deploy: core and pages pins, then the plugin binary.
release >/dev/null; commit "deploy: refresh pins"
printf '#!/bin/sh\n[ -z "$FAKE_FAIL" ] || exit 1\necho "fake awm $*"\n' > "$tmp/awm"
cv="$(v core_version)" pgv="$(v pages_version)"
cp "$tmp/awm" "$node/.awm/artifacts/awm-$cv"
tar -C "$node" -czf "$node/.awm/artifacts/awm-$pgv.tar.gz" awm/services/svc/pages/ui
(cd "$node" && . scripts/pins.sh \
    && write_pin scripts/core.pin core "$cv" "awm-$cv" .awm/artifacts \
    && write_pin scripts/pages.pin pages "$pgv" "awm-$pgv.tar.gz" .awm/artifacts)
commit "core pins"
check "a dry run places nothing and names the restart" \
    'deploy --dry-run | grep -q -- "--reload-restart hello --dry-run" && [ ! -e "$node/.awm/bin/hello" ]'
check "a failed deploy keeps the restart pending" '! FAKE_FAIL=1 deploy >/dev/null && [ -e "$node/.awm/bin/.hello.restart" ]'
check "the binary lands at <AWM_DIR>/bin/hello" '[ "$("$node/.awm/bin/hello")" = "hello two" ]'
check "the next deploy restarts the pending plugin" 'deploy | grep -q -- "--reload-restart hello$" && [ ! -e "$node/.awm/bin/.hello.restart" ]'
check "an unchanged plugin is not replaced or restarted" '! deploy | grep -q -- "--reload-restart"'
echo four > "$node/core/plugins/hello/x"; commit "hello four"
check "a stale plugin pin stops the deploy" 'out="$(deploy 2>&1)"; [[ $out == *"hello.pin names"*"refresh-pins.sh"* ]]'
rm "$node/scripts/plugins/hello.pin"; commit "no pin"
check "a missing plugin pin stops the deploy" 'out="$(deploy 2>&1)"; [[ $out == *"no scripts/plugins/hello.pin"* ]]'

[ "$fails" -eq 0 ] && echo "all passed" || { echo "$fails failed"; exit 1; }

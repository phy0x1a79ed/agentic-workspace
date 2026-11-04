#!/usr/bin/env bash
# The node-local deploy, run with this checkout's core binary. A node still on the Python
# gateway has no Rust `awm` on its PATH, so the binary comes from AWM_BIN or is built here.
# Arguments pass through to `awm deploy`.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
bin="${AWM_BIN:-$root/core/target/x86_64-unknown-linux-musl/release/awm}"
if [ -z "${AWM_BIN:-}" ] && command -v docker >/dev/null 2>&1; then
    "$root/scripts/rust-build.sh" build --release --target x86_64-unknown-linux-musl -p awm-cli
fi
[ -x "$bin" ] || { echo "no core binary at $bin: set AWM_BIN, or install docker to build it" >&2; exit 1; }
exec "$bin" deploy --root "$root" --bin "$bin" "$@"

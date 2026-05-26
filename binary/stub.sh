#!/bin/sh
# Pre-rustup stub served by tools/serve.py in place of the real `probe`
# binary. Mirrors src/main.rs behavior closely enough to exercise the
# install-and-run UX (download → chmod → exec → consent prompt → exit).
set -e

VERSION="0.0.1-stub"
USER_ARG="${1:-an awm operator}"

printf 'probe v%s (stub) — vagrant-shell pair-debug binary\n' "$VERSION" >&2
printf '\n' >&2
printf 'probe is about to expose this shell to %s.\n' "$USER_ARG" >&2
printf 'Continue? [y/N] ' >&2

read -r answer || { printf 'could not read consent — aborting.\n' >&2; exit 2; }
case "$answer" in
    y|Y|yes|YES) ;;
    *) printf 'aborted by user.\n' >&2; exit 1 ;;
esac

printf '\n' >&2
printf '[stub] consent granted; this is the pre-rustup shell-script stub.\n' >&2
printf '[stub] the real Rust binary lands once cargo is installed.\n' >&2

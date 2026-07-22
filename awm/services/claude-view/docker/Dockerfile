# Build environment for the upstream claude-view server binary.
#
# WHY THIS EXISTS AT ALL. Upstream ships prebuilt linux-x64 binaries, and the
# plan was to just consume them. They do not run here: CI builds them on
# ubuntu-24.04, so they carry GLIBC_2.38/2.39 version requirements, and this
# host is Ubuntu 22.04 with glibc 2.35. The dynamic loader rejects the binary
# outright — not a symbol at a time, but on the version-set check, so there is
# no env var or flag that gets past it. The binary must therefore be compiled
# against something this host can load.
#
# WHY MUSL / ALPINE. Compiling on the host works but leaves the result pinned to
# "whatever glibc this box happens to have", which is the exact fragility that
# caused the problem in the first place. Alpine's native target is
# x86_64-unknown-linux-musl, where Rust links crt-static by default — the output
# is a fully static binary with no libc dependency of any kind. That makes the
# whole glibc-floor question permanently moot and lets the same artifact run on
# this host, in a container, or on a cluster node without a rebuild.
#
# It is also *native*, not cross: no linker overrides, no CC/AR juggling, none
# of the cross-toolchain gotchas that make the multi-target darwin builders
# fussy. The one cost is musl's allocator, which is slower than glibc's under
# heavy multithreaded load — claude-view's indexer is exactly that, so the build
# script benchmarks the result against the recorded glibc baseline rather than
# assuming parity.
#
# The tag below is only a BOOTSTRAP. claude-view ships a `rust-toolchain.toml`
# pinning channel 1.94.1, and rustup honours it — so the compiler that actually
# builds the binary is upstream's own pinned version, downloaded on first run
# (as the musl-host variant) and then cached in the cargo volume. That is
# stronger provenance than pinning a toolchain ourselves: we cannot drift from
# what upstream compiles with, even accidentally. This tag just needs to be new
# enough for rustup to bootstrap from.
FROM rust:1.95-alpine3.22

# build-base  : gcc + headers, for rusqlite's bundled SQLite C sources
# openssl-dev + openssl-libs-static : openssl-sys links this statically.
#   claude-view configures reqwest for rustls, but its cargo-hakari
#   `workspace-hack` crate lists reqwest *without* default-features = false, so
#   feature unification pulls `default-tls` → native-tls → openssl back in for
#   the whole graph. Hence a real OpenSSL is required despite the rustls intent.
# pkgconf     : how openssl-sys locates the above.
RUN apk add --no-cache \
        build-base \
        openssl-dev \
        openssl-libs-static \
        pkgconf \
        git

# Force the static libs; without this openssl-sys prefers the shared objects and
# the "fully static" property quietly evaporates.
ENV OPENSSL_STATIC=1 \
    PKG_CONFIG_ALL_STATIC=1 \
    CARGO_TERM_COLOR=always

# Cargo's registry + git caches live here; build.sh mounts a named volume over
# it so repeat builds do not re-download the ~400 crate dependency tree.
ENV CARGO_HOME=/cargo

WORKDIR /src

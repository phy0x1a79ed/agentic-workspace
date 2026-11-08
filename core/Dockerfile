# mizar's Rust build container (install.md R113). Built and run by scripts/rust-build.sh.
FROM rust:1.99.1-bookworm
RUN rustup target add x86_64-unknown-linux-musl \
 && rustup component add clippy rustfmt \
 && apt-get update \
 && apt-get install -y --no-install-recommends musl-tools protobuf-compiler libprotobuf-dev \
 && rm -rf /var/lib/apt/lists/*
# The script runs the container as the host user, so the cache must be writable by any uid.
RUN mkdir -p /cargo/registry /cargo/git && chmod -R 1777 /cargo
ENV CARGO_HOME=/cargo
WORKDIR /src

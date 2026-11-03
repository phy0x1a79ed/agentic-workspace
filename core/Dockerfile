# mizar's Rust build container (install.md R113). Built and run by scripts/rust-build.sh.
FROM rust:1.99.1-bookworm
RUN rustup target add x86_64-unknown-linux-musl \
 && rustup component add clippy rustfmt \
 && apt-get update \
 && apt-get install -y --no-install-recommends musl-tools \
 && rm -rf /var/lib/apt/lists/*
ENV CARGO_HOME=/cargo
WORKDIR /src

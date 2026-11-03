# The bare node of the install test (install.md R118): a plain OS with no awm in it.
# It holds no secret, no fleet detail and no Claude Code (R119 items 1 and 2).
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates openssl \
 && rm -rf /var/lib/apt/lists/*

# The image an isolated session runs in (fleet.md R109). It carries no Claude
# install: cch mounts the host's binary read only at /opt/claude/claude, so the
# container runs the same version as the host.
#
# The session runs as the container's root, which rootless Docker maps to the
# host user, so the files it writes in the worktree belong to that user.
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git tmux ripgrep less procps \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /home/agent/.claude /opt/claude \
 && git config --system --add safe.directory '*'

ENV HOME=/home/agent \
    CLAUDE_CONFIG_DIR=/home/agent/.claude \
    IS_SANDBOX=1 \
    TERM=xterm-256color \
    LANG=C.UTF-8

# syntax=docker/dockerfile:1.7
# Factorio Space Age appliance -- ONE image, two roles.
#
# The full (graphical) build also serves: `--start-server` is how anyone hosts
# from a normal install, and it initialises no graphics at all, so the host
# needs no display. The headless build, by contrast, carries no `--mp-connect`
# and can never join. So the appliance downloads the full build once and the
# same image runs as the host (FACTORIO_ROLE=host -> supervise.py) and as a
# seat (FACTORIO_ROLE=seat -> seat.sh). Host and seats therefore cannot drift
# out of version or mod sync -- the failure mode of every hand-built Factorio
# multiplayer setup.
#
# The full build is behind factorio.com account auth. Credentials arrive as a
# BuildKit secret and are never persisted into a layer -- see ./build.sh.
FROM debian:bookworm-slim

# Pin to a specific release so the server matches every client that joins it,
# ours and the user's Steam copy alike. Bump in lockstep.
ARG FACTORIO_VERSION=2.1.17
# `alpha` is the base game only: its data/ carries just base + core. The Space
# Age mods a server bundles (space-age, quality, elevated-rails, recycler) are
# absent, so an `alpha` client's mod set can never match this world's and the
# join is refused with ModsMismatch. `expansion` is the same full build WITH
# those mods, graphics included -- it is the only variant that works here.
ARG FACTORIO_VARIANT=expansion

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl xz-utils ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*

# ~4.4 GB compressed. Kept as its own layer so the runtime dependency list
# below can be iterated without re-downloading.
RUN --mount=type=secret,id=factorio_creds,required=true \
    set -eu; \
    . /run/secrets/factorio_creds; \
    curl -fsSL -o /tmp/factorio.tar.xz \
        "https://factorio.com/get-download/${FACTORIO_VERSION}/${FACTORIO_VARIANT}/linux64?username=${FACTORIO_USER}&token=${FACTORIO_TOKEN}"; \
    tar -xJf /tmp/factorio.tar.xz -C /opt; \
    rm /tmp/factorio.tar.xz

# The graphical binary links X, GL and audio libraries the headless one did
# not. Xvfb + libgl1-mesa-dri supply a seat's private display and its software
# rasterizer (this host has no X server and no GL path to the GPU); xdotool
# unmaps the window, which is what makes a seat cost 0.14 rather than 1.5 cores.
# A host container loads these and uses none of them.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11-utils xdotool \
        libgl1 libglx-mesa0 libgl1-mesa-dri libegl1 \
        libx11-6 libxcursor1 libxinerama1 libxrandr2 libxi6 libxext6 libxrender1 \
        libxkbcommon0 libxcb1 \
        libasound2 libpulse0 \
        libstdc++6 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /factorio/config /factorio/saves /opt/factorio/script-output

# Server + map-gen config baked in (override via a mounted volume if desired).
COPY config/server-settings.json   /factorio/config/server-settings.json
COPY config/map-gen-settings.json  /factorio/config/map-gen-settings.json
# Engine config. Shared by both roles: its [path] section is the stock tarball
# layout, and the [graphics] tuning it carries is dead weight for a host that
# never initialises graphics.
COPY config/config.ini             /opt/factorio/config/config.ini
# Enabling the Space Age mods is just listing them here. A joining peer's mod
# set must match the server's exactly -- which one image gives us for free.
COPY config/mod-list.json          /opt/factorio/mods/mod-list.json
# Companion mod: the script-controlled bodies (spawn/move/observe).
COPY mods/game-bot-control         /opt/factorio/mods/game-bot-control

COPY supervise.py     /factorio/supervise.py
COPY seat.sh          /factorio/seat.sh
COPY entrypoint.sh    /factorio/entrypoint.sh

# Host role only. Game traffic (UDP) + supervisor control surface (TCP).
EXPOSE 12140/udp 12142/tcp

# No VOLUME for /factorio/saves: docker-compose.yml mounts the named saves
# volume onto the host container explicitly, and a VOLUME here would also make
# every transient seat leave an anonymous volume behind on removal.

ENTRYPOINT ["/factorio/entrypoint.sh"]

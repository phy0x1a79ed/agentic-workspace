# Factorio Space Age headless appliance.
# Debian + pinned Factorio headless + a stdlib-only python supervisor.
FROM debian:bookworm-slim

# Pin to a specific stable release so the server matches the Steam client that
# joins it. Bump in lockstep with the desktop client.
ARG FACTORIO_VERSION=2.0.77

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl xz-utils ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*

# Download + unpack the headless tarball. The bundled Space Age mod dirs
# (space-age, quality, elevated-rails) live under data/ and are left in place;
# they are inert until enabled in mod-list.json (below).
RUN curl -fSL "https://factorio.com/get-download/${FACTORIO_VERSION}/headless/linux64" \
        -o /tmp/factorio.tar.xz \
    && tar -xJf /tmp/factorio.tar.xz -C /opt \
    && rm /tmp/factorio.tar.xz

RUN mkdir -p /factorio/config /factorio/saves /opt/factorio/mods

# Server + map-gen config baked in (override via a mounted volume if desired).
COPY config/server-settings.json   /factorio/config/server-settings.json
COPY config/map-gen-settings.json  /factorio/config/map-gen-settings.json
# Enabling the Space Age mods is just listing them here.
COPY config/mod-list.json          /opt/factorio/mods/mod-list.json

COPY supervise.py /factorio/supervise.py

# Game traffic (UDP) + supervisor control surface (TCP).
EXPOSE 12140/udp 12142/tcp

VOLUME ["/factorio/saves"]

ENTRYPOINT ["python3", "-u", "/factorio/supervise.py"]

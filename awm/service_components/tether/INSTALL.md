# Installing the tether component

## Purpose & Contents

This file is the operator's contract for the tether component and the two plugins that use it: the role each host plays, the environment each role reads, how a host gets the tether binaries, and what must agree across hosts. The tether protocol, its Rust source and its launchers live in the tether repository (`github.com/phy0x1a79ed/tether`).

## What it carries

A channel is a room or a machine (`docs/redux/channels.md` R110). The public host's `relay` plugin keeps every room and supervises `tether-relay`. Each member node's `channel` plugin holds its sessions' rooms through `relay`, and supervises `tether-operator`, which mints machine sessions. The tether repository builds and publishes the binaries. Each host fetches the version `RELEASE` names.

## Two roles

Each plugin's `run.sh` sets `AWM_TETHER_ROLE`.

| Role | Plugin | Supervises |
|---|---|---|
| `operator` | `channel`, on every member node | `tether-operator`, which mints machine sessions |
| `relay` | `relay`, on the public host | `tether-relay`, which carries tether sessions and rooms and reads nothing |

A machine channel opens only on an `operator` host.

## Install

Run the plugin's `install.sh`. It installs the plugin, this component and their dependencies into the `awm` env (override with `AWM_ENV`), and fetches the binaries once. `run.sh` fetches again on every start.

A failed fetch is not fatal. The binaries in place keep running, and `channel status` on a machine reports the version lag under `tether.release`. `AWM_TETHER_BIN` points the plugin at a local cargo target directory and skips the fetch.

## Environment

| Env var | Read by | Meaning |
|---|---|---|
| `AWM_TETHER_ISSUE_TOKEN` | both roles | the bearer that makes opening a session or a room operator-only |
| `AWM_ROOM_RELAY` | relay | the tether relay's room routes (default `http://127.0.0.1:$AWM_TETHER_PORT`) |
| `AWM_ROOM_LINK` | relay | the edge root a room's link names (default `https://nexus.tony-xy-liu.com`) |
| `AWM_RELAY_LEASE_S` | relay | how long a member stays without its node renewing it (default 180) |
| `TETHER_RELAY` | operator daemon | the relay's tether mount |
| `AWM_TETHER_PORT` | relay | the loopback port `tether-relay` binds (default 12520) |
| `AWM_TETHER_ASSETS` | relay, fetch | the directory the relay serves the launchers and clients from (default `<AWM_DIR>/services/tether/assets`) |
| `AWM_TETHER_ROOM_STATE` | relay | where persistent rooms keep their files across a restart (default `<AWM_DIR>/services/tether/rooms`) |
| `AWM_TETHER_CLIENT_IP_HEADER` | relay | the header that carries the caller's address (default `cf-connecting-ip`) |
| `AWM_EDGE_TETHER` | httpsfront | `1` mounts `/tether` and `/room` on the public edge |
| `AWM_TETHER_BIN` | plugin, fetch | a local build, for development only |

`AWM_TETHER_ISSUE_TOKEN` is a secret. Where the gateway spawns a plugin, it comes from the node's `secrets.env`. Where `relay` runs as a singleton under its own user, its unit loads it with `LoadCredential=secrets.env:/etc/awm/secrets/relay.env`, and `awm.gatewayclient.credentials` reads it. The binaries, the assets and the rooms live under the service's `AWM_DIR`, which a singleton's unit points at its own state directory.

**CAUTION** Every host needs the same `AWM_TETHER_ISSUE_TOKEN`. The relay refuses to start without one. The operator daemon starts without one, so `channel status` can say why the other verbs fail.

**CAUTION** The edge's path grammar (`awm/services/httpsfront/awm/httpsfront/tether.py`) must match the relay's parsers exactly: slots, tokens, and room names of three or six list words. `awm/services/httpsfront/tests/test_tether_paths.py` writes both boundaries out.

**CAUTION** The relay keys its per-address budget on the client address header. The edge forwards it on plain requests and on websocket upgrades alike. An edge that drops it on an upgrade puts every room socket in one shared budget.

## Rooms

`relay` keeps each room, its members and every message no member has fetched in `<AWM_DIR>/services/relay/channels.db`, so a restart loses none. An encrypted room's keys, plaintext and transcript stay on the member's node. The host keeps only the sealed text.

**CAUTION** `relay` opens no connection of its own (`docs/redux/fleet.md` R79). Members pull with `relay feed` through the host's door. A plugin on the public host that needs reflection, a door or an edge breaks this.

## The operator daemon

The relay dies with its plugin. The operator daemon does not: it runs in a transient user unit, `awm-tether-operator-<hash>`, outside the gateway's cgroup, so a deploy keeps every live session. The next plugin process attaches to it. The plugin replaces it only for a new release with no session live. A reboot ends it.

**CAUTION** The unit name and the control socket derive from the state path, `<AWM_DIR>/services/tether`. Moving it orphans the running daemon and its sessions.

**CAUTION** The daemon reads its environment once, at launch. To apply a changed env file, stop the unit with `systemctl --user stop awm-tether-operator-<hash>`. The plugin starts a new daemon within a few seconds.

**WARNING** Stopping the unit ends every live machine session. Each holding session gets an end line, and its channel closes.

## The owner's side installs nothing

The owner runs one line that `channel open kind=machine` returns for their declared `os`. It fetches the client, verifies its SHA-256, runs it, and stops. It installs nothing. The tether repository's `README.md` covers the client.

# Installing the `channel` plugin

## Purpose & Contents

This file is the operator's contract for the `channel` plugin: the role a host plays, the environment each role reads, how a host gets the tether binaries, and what must agree across hosts. The verb surface is `awm channel --help`. The tether protocol, its Rust source and its launchers live in the tether repository (`github.com/phy0x1a79ed/tether`).

## What it carries

A channel is a room or a machine (`docs/redux/channels.md` R110). A room needs only the relay's HTTP routes. A machine is one tether session, driven by the `tether-operator` daemon this plugin supervises. The tether repository builds and publishes the binaries. Each host fetches the version `RELEASE` names.

## Two roles

`AWM_TETHER_ROLE` decides which binary a host supervises.

| Role | Set on | Supervises |
|---|---|---|
| `operator` (default) | every node that holds channels | `tether-operator`, which mints machine sessions |
| `relay` | the public host | `tether-relay`, which carries tether sessions and rooms and reads nothing |

A machine channel opens only on an `operator` host. Rooms open on either role, because a room is an HTTP call to the relay.

## Install

Run `bash install.sh`. It installs the plugin and its dependencies into the `awm` env (override with `AWM_ENV`), checks that `spake2` imports, and fetches the binaries once. `run.sh` fetches again on every start.

A failed fetch is not fatal. The binaries in place keep running, and `channel status` on a machine reports the version lag under `tether.release`. `AWM_TETHER_BIN` points the plugin at a local cargo target directory and skips the fetch.

## Environment

| Env var | Read by | Meaning |
|---|---|---|
| `AWM_TETHER_ROLE` | plugin | `operator` or `relay` |
| `AWM_TETHER_ISSUE_TOKEN` | both roles | the bearer that makes opening a session or a room operator-only |
| `AWM_ROOM_RELAY` | plugin | the edge root that serves `/room` (default `https://nexus.tony-xy-liu.com`) |
| `TETHER_RELAY` | operator daemon | the relay's tether mount |
| `AWM_TETHER_PORT` | relay | the loopback port it binds (default 12520) |
| `AWM_TETHER_ASSETS` | relay, fetch | the directory the relay serves the launchers and clients from |
| `AWM_TETHER_ROOM_STATE` | relay | where persistent rooms keep their files across a restart (default `.awm/services/channel/tether/rooms`) |
| `AWM_TETHER_CLIENT_IP_HEADER` | relay | the header that carries the caller's address (default `cf-connecting-ip`) |
| `AWM_EDGE_TETHER` | httpsfront | `1` mounts `/tether` and `/room` on the public edge |
| `AWM_TETHER_BIN` | plugin, fetch | a local build, for development only |

**CAUTION** Every host needs the same `AWM_TETHER_ISSUE_TOKEN`. The relay refuses to start without one. The operator daemon starts without one, so `channel status` can say why the other verbs fail.

**CAUTION** The edge's path grammar (`awm/services/httpsfront/awm/httpsfront/tether.py`) must match the relay's parsers exactly: slots, tokens, and room names of three or six list words. `awm/services/httpsfront/tests/test_tether_paths.py` writes both boundaries out.

**CAUTION** The relay keys its per-address budget on the client address header. The edge forwards it on plain requests and on websocket upgrades alike. An edge that drops it on an upgrade puts every room socket in one shared budget.

## The operator daemon

The relay dies with the plugin. The operator daemon does not: it runs in a transient user unit, `awm-tether-operator-<hash>`, outside the gateway's cgroup, so a deploy keeps every live session. The next plugin process attaches to it. The plugin replaces it only for a new release with no session live. A reboot ends it.

**CAUTION** The unit name and the control socket derive from the state path, `.awm/services/channel/tether`. Moving it orphans the running daemon and its sessions.

**CAUTION** The daemon reads its environment once, at launch. To apply a changed env file, stop the unit with `systemctl --user stop awm-tether-operator-<hash>`. The plugin starts a new daemon within a few seconds.

**WARNING** Stopping the unit ends every live machine session. Each holding session gets an end line, and its channel closes.

## The owner's side installs nothing

The owner runs one line that `channel open kind=machine` returns for their declared `os`. It fetches the client, verifies its SHA-256, runs it, and stops. It installs nothing. The tether repository's `README.md` covers the client.

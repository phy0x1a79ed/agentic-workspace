# channel

## Purpose & Contents

This file is the operator's contract for the channel plugin: the role each host plays, the environment each role reads, how a host gets the tether binaries, and what must agree across hosts. The tether protocol, its Rust source and its launchers live in the tether repository (`github.com/phy0x1a79ed/tether`). The verbs are in `awm channel describe`.

## Two halves, one binary

A channel is a room or a machine (`docs/redux/channels.md` R110). The binary serves the service `AWM_SERVICE_NAME` names.

| Service | Runs on | Keeps | Supervises |
|---|---|---|---|
| `channel` | every member node | its sessions' channels in `services/channel/` | `tether-operator`, which mints machine sessions |
| `relay` | the public host, by profile `relay-host` | every room and member in `services/relay/` | `tether-relay`, which carries tether sessions and rooms |

The public host excludes `channel/channel`. Callers name the singleton tool `relay` in `edges.yml`.

## The tether binaries

`RELEASE` pins the tether release, and the binary embeds the pin. On start each half fetches its role's assets when `services/tether/bin/VERSION` differs from the pin. It checks every file against the release's `SHA256SUMS`, moves them into place, and stamps `VERSION` last. A failed fetch is not fatal: the binaries in place keep running, and the version lag shows under `tether.release` in `channel status` on a machine and in `relay tether`. `AWM_TETHER_BIN` points at a local build and skips the fetch.

## Environment

| Env var | Read by | Meaning |
|---|---|---|
| `AWM_TETHER_ISSUE_TOKEN` | relay, tether-relay | the bearer for the tether relay's `/issue`, `/status` and `POST /room` |
| `AWM_ROOM_RELAY` | relay | the tether relay's room routes (default `http://127.0.0.1:$AWM_TETHER_PORT`) |
| `AWM_ROOM_LINK` | relay | the edge root a room's link names (default `https://nexus.tony-xy-liu.com`) |
| `AWM_RELAY_LEASE_S` | relay | how long a member stays without its node renewing it (default 180) |
| `TETHER_RELAY` | operator daemon | the relay's tether mount |
| `AWM_TETHER_PORT` | relay, tether-relay | the loopback port `tether-relay` binds (default 12520) |
| `AWM_TETHER_ASSETS` | relay, tether-relay | the directory the relay serves the launchers and clients from (default `<AWM_DIR>/services/tether/assets`) |
| `AWM_TETHER_ROOM_STATE` | tether-relay | where persistent rooms keep their files across a restart (default `<AWM_DIR>/services/tether/rooms`) |
| `AWM_TETHER_CLIENT_IP_HEADER` | tether-relay | the header that carries the caller's address (default `cf-connecting-ip`) |
| `AWM_TETHER_BIN` | both | a local build, for development only |

`AWM_TETHER_ISSUE_TOKEN` is a secret. Where the gateway spawns the plugin, it comes from the node's `secrets.env`. Where `relay` runs as a singleton under its own user, its unit loads it with `LoadCredential=secrets.env:/etc/awm/secrets/relay.env`.

**CAUTION** Only the public host holds `AWM_TETHER_ISSUE_TOKEN` (`docs/redux/channels.md` R147). The edge forwards none of the three routes it gates. A member node's operator daemon gets each slot, and restores a held one, by running `channel issue`, which calls `relay issue` through the public host's door on the `channel` service's account. A node outside `relay`'s callers in `edges.yml` gets no slot.

**CAUTION** The edge's path grammar (httpsfront) must match the relay's parsers exactly: slots, tokens, and room names of three or six list words.

**CAUTION** The relay keys its per-address budget on the client address header. The edge forwards it on plain requests and on websocket upgrades alike.

## Rooms

`relay` keeps each room, its host token, its members and every message no member has fetched in `services/relay/channels.db`, so a restart of the plugin loses none. An encrypted room's keys, plaintext and transcript stay on the member's node. The public host keeps only the sealed text. An encryption offer lives in memory only, so an offer open when `channel` restarts is dropped, and its holder gets a line saying so.

**CAUTION** `relay` opens no connection of its own (`docs/redux/fleet.md` R79). Members pull with `relay feed` through the public host's door. A need on reflection, a door or an edge would refuse the plugin there, so the plugin declares none.

## The adopted binaries

Neither binary dies with its plugin. Each runs in a transient user unit outside the gateway's cgroup: `awm-tether-operator-<hash>` or `awm-tether-relay-<hash>`. The next plugin process attaches to the running binary: the operator answers on its control socket, the relay on `/health`. The plugin replaces a binary only when it is stale and no session or room is live. Stale means a fetch replaced its file, it runs another release than the one installed, or, for the operator, its `TETHER_ISSUER` is not this plugin's `channel issue`. For the relay the plugin counts live rooms from `/status`, which needs the bearer. Without the bearer the relay is never replaced. A reboot ends both. On a host with no user manager the binary runs in its own session, `status` shows a null `unit`, and a restart of the plugin's own systemd unit ends the binary.

**CAUTION** The unit name and the control socket derive from the state path, `<AWM_DIR>/services/tether`, as `sha1(path)[:8]`. Moving the path orphans the running daemon and its sessions.

**CAUTION** The daemon reads its environment once, at launch. `daemon.env` holds an allowlist of the plugin's environment and no secret. A secret reaches the unit through `systemd-run --setenv`, which keeps it in the user manager's runtime directory. To apply a changed environment, stop the unit with `systemctl --user stop awm-tether-operator-<hash>`. The plugin starts a new daemon within a few seconds.

**CAUTION** The daemon outlives `channel`, so `channel issue` reads the token file of `channel`'s current spawn on each call. While `channel` is down, an invite fails, and a restore retries until the resume window ends.

**WARNING** Stopping the unit ends every live machine session. Each holding session gets an end line, and its channel closes.

## The owner's side installs nothing

The owner runs one line that `channel open kind=machine` returns for their declared `os`. It fetches the client, verifies its SHA-256, runs it, and stops. The tether repository's `README.md` covers the client.

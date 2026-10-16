# Installing the `tether` service

## Purpose & Contents

This file is the operator's contract for the awm `tether` service: what the
folder ships, which role a host plays, the environment each role reads, and how
a host gets the binaries it supervises.

What belongs here: the two roles, every environment variable and who sets it,
the install step, the release pin, and the failure each host shows when a binary
is missing. What does not belong here: the Rust source, the build and the
launchers, which live in the tether repository (`github.com/phy0x1a79ed/tether`);
the protocol, which is that repository's `PROTOCOL.md`; and the verb surface,
which `awm tether --help` renders from the live manifest.

## What this is

A consent-gated remote-assistance session between an **operator** and an
**owner**, bounced through a **relay** neither side has to be reachable from.
The operator mints an invite code. The owner reads it into one piped command,
answers a prompt, and watches the session until either side cuts it.

This folder holds a thin Python adapter and a one-line `RELEASE` pin. The three
binaries that do the work — the operator daemon, the relay, and the owner's
client — are built and published by the tether repository. Each host fetches the
version `RELEASE` names from that repository's GitHub release.

## Two roles, one folder

The same service runs on two kinds of host. `AWM_TETHER_ROLE` decides which.

| Role | Set on | Supervises | Needs |
|---|---|---|---|
| `operator` (default) | your own node | `tether-operator`, which mints invites and drives a session | its fetched binary, and the relay's bearer |
| `relay` | the public host | `tether-relay`, which pairs two sockets and reads nothing | its fetched binaries, its own bearer, the edge mount |

Neither host needs a toolchain. Each fetches its role's binaries from the
GitHub release on every start (see *Fetching the binaries*). The relay also
takes every client and both launchers, because it serves them as the fallback
when GitHub cannot be reached.

## Install

    bash install.sh

The script editable-installs `gatewayclient` and this service into the `awm`
env. Override the env with `AWM_ENV=<name>`. It writes a gitignored
`.runtime-env` sidecar baking `AWM_PYTHON` = the env's absolute interpreter, so
the gateway can respawn the service under systemd's minimal PATH, where `mamba`
is absent. It then runs `fetch-release.sh` once, so a fresh install comes up
running rather than waiting for the first restart. A host that cannot reach the
release still installs, and reports the missing binaries through
`awm tether status`.

## Fetching the binaries

`RELEASE` holds one tag, such as `v0.1.0`. It is the single pin that decides
which tether every node runs.

`fetch-release.sh` reads `RELEASE`, compares it with the `VERSION` stamp beside
the installed binaries, and does nothing when they match. On a mismatch it
downloads `SHA256SUMS` and this host's role from the matching GitHub release,
verifies every file against `SHA256SUMS`, and swaps the binaries in. `run.sh`
calls it before the adapter registers, so the version a node runs follows the
pin with no hand-run step. A deploy that changes `RELEASE` restarts the service,
and the restart fetches.

A failed fetch is not fatal. It leaves the binaries already in place untouched
and leaves the `VERSION` stamp behind the pin, which `awm tether status` reports
as a version lag. A GitHub outage never takes the relay down.

To build the binaries, cut a release, or change the protocol, work in the tether
repository. awm and tether ship in lockstep — the operator decodes frames the
clients send — so a protocol change bumps `RELEASE` in the same awm change that
needs it.

**CAUTION** `AWM_TETHER_BIN` points the service at a local build and skips the
fetch entirely. Set it to a tether checkout's cargo target directory to develop
against uncommitted binaries. `awm tether status` then reports the build as an
override rather than a version lag, because there is no pinned version to
compare.

## Environment

The gateway injects the first three. Everything else comes from the host's env
file, which is `<workspace>/.awm/env` on your own node and `/etc/awm/env` on
the public host.

| Env var | Read by | Meaning |
|---|---|---|
| `AWM_HUB_URL` | adapter | base URL of the running gateway |
| `AWM_SERVICE_NAME` | adapter | this service's name (= `tether`) |
| `AWM_SERVICE_ID` | adapter | assigned on respawn so reconnect targets the same control URL |
| `AWM_TETHER_ROLE` | adapter | `operator` (default) or `relay` |
| `AWM_TETHER_BIN` | adapter, fetch | a local build directory, which overrides the fetch (development only) |
| `AWM_TETHER_ISSUE_TOKEN` | both roles | the bearer that makes a session operator-only |
| `AWM_TETHER_PORT` | relay | the loopback port it binds (default 12520) |
| `AWM_TETHER_ASSETS` | relay, fetch | the directory holding both launchers and the client downloads |
| `AWM_TETHER_MAX_SESSIONS` | relay | cap on live sessions |
| `AWM_TETHER_CLIENT_IP_HEADER` | relay | which header carries the owner's address (default `cf-connecting-ip`) |
| `AWM_EDGE_TETHER` | httpsfront | set `1` to mount the relay at `/tether` on the public edge |
| `TETHER_REPO`, `TETHER_GITHUB_BASE` | fetch | override the repository or the release download base, for a mirror or a test |
| `TETHER_RELEASE_DIR` | fetch | copy the assets from a local directory instead of downloading, for a test with no published release |

The relay's port is declared once in `awm.config`, because two processes must
agree on it and neither owns it. The owner's client reads its own environment
(the shell it runs, a proxy, a test relay); that is documented with the client
in the tether repository.

**CAUTION** `AWM_TETHER_ISSUE_TOKEN` is the whole of "only an operator may open
a session". Both hosts need **the same value**. The relay refuses to start
without one, which is the right direction: a relay that came up without a
bearer would accept sessions from anybody. The operator's daemon does start
without one, which is also the right direction: `awm tether status` is the verb
whose job is to explain why the others will not work, and a daemon that exited
could not answer it.

**CAUTION** The edge's path grammar and the relay's own parsers must agree
exactly. `awm/services/httpsfront/awm/httpsfront/tether.py` decides which paths
the public door opens, and it refuses everything else before consulting any
upstream. A grammar looser than `Slot::parse` lets a malformed slot reach the
relay. A grammar tighter than it turns a legitimate invite into a 404 that
names no cause. `awm/services/httpsfront/tests/test_tether_paths.py` writes
both boundaries out, so a change on either side has to change that file too.

## Run

You never invoke the service by hand in normal operation. The gateway discovers
this folder, starts it with `bash run.sh`, and supervises the child. No auth:
the registration handshake carries no token.

The child dies with this process, which is deliberate. A tether that outlived
its supervisor would be the persistence this tool refuses to have.

Run `awm tether --help` for the verbs. Three of them answer locally rather than
reaching the daemon, because they are the three that have to work when the
daemon is the thing that is wrong: `status` reports the child's process state
before merging whatever the daemon says, `logs` reads the file, and `drain`
reads this host's own buffer of what the session did.

One shape is worth knowing before reading the list. **Asking is a call and
hearing back is a stream.** `run` and `shell` start something and answer with a
task id immediately; the output, the exit and anything either person said arrive
as events. Read them with `drain`, which with no cursor means "what is new since
I last asked" and remembers that per caller, or follow them live on the
service's `session` topic.

## The owner's side installs nothing

The owner runs one line and holds no key material:

    curl -fsSL https://nexus.tony-xy-liu.com/tether | sh -s acre anchor kettle

Windows has its own launcher at its own address:

    & ([scriptblock]::Create((irm https://nexus.tony-xy-liu.com/tether/win))) acre anchor kettle

`invite` requires `os`: `windows`, `wsl`, `linux` or `macos`. It has no
default, because a guessed OS hands the owner a line their shell cannot run.
`windows` gets the PowerShell line and the other three get the `sh` line. The
daemon builds only the `sh` line, so the adapter builds the Windows one from it.

**CAUTION** The declared `os` lives in the adapter's memory, not in the daemon.
That is safe only because the daemon and its sessions die with the adapter. The
owner's client reports its real platform in its `Hello`, which `status` shows
beside the declaration. A `wsl` session reports `linux`, which counts as
agreement.

The launcher fetches the client for that machine from the GitHub release first
and from the relay only when GitHub is unreachable, verifies the download's
SHA-256 against a baked value, runs it, and stops. It installs nothing to PATH
and sets up no autostart. The owner's full client behaviour — the two-run
consent path for a terminal-less owner, the session record, the portability
guarantees — is documented in the tether repository's `README.md` and
`PROTOCOL.md`.

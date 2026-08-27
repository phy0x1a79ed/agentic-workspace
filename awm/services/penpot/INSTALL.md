# penpot

Supervision of the Penpot docker-compose stack — the diagramming service
that replaces drawio. Upstream is a Penpot fork in the separate `penpot`
project (`projects/penpot/dev`), built and run as five containers by
`docker compose`. This service starts/stops/restarts that stack and reports
its health; it never binds a public port and carries no HTTP or WebSocket
traffic of its own — a separate httpsfront wiring proxies `/penpot` to the
running frontend container.

## Purpose & Contents

This file holds the decisions a reader cannot recover from the code: why
this service does not touch per-user scaffolding, why the port bindings it
assumes matter for safety, and what it does and does not manage. Penpot's
own architecture belongs to `projects/penpot` and upstream's docs. The
compose files themselves — image tags, memory caps, `PENPOT_FLAGS` — live in
`projects/penpot/dev/docker/images/`, not here; this file covers only the
boundary between awm and the stack.

## This service does not touch per-user scaffolding

**Explicitly: no `userroot`, no `add-user.sh`, no per-user scope.** Penpot is
a multi-tenant application with its own accounts, teams and Postgres
database — sign-up, login, and team membership are all Penpot's own concern,
reached through its own UI once a browser gets to `/penpot`. Adding a person
to Penpot is a Penpot action (register, or an admin invite), never an awm
scaffolding step. This mirrors Trilium's boundary in spirit but lands on the
opposite side of it: Trilium is *single-user per instance* and needed awm to
say who was asking; Penpot is multi-tenant per instance and needs nothing
from awm beyond "let this session through to the front door."

Concretely, this means:

- No `awm.config.userroot` import anywhere in this package.
- No entry in `scripts/sirius/add-user.sh`.
- No per-user directory under `projects/userdata/`.
- The compose stack's Postgres database is the *only* place Penpot user
  state lives — back it up like any other production database if that ever
  matters, not through awm's DVC-pinned scope data model.

## Configuration — built for a second host

`awm/penpot/stack.py`'s `StackConfig` is entirely environment-driven, not
hardcoded to this box's paths:

| Env var | Default | What it names |
|---|---|---|
| `PENPOT_COMPOSE_DIR` | `<workspace>/projects/penpot/dev/docker/images` | Where `docker compose` runs from |
| `PENPOT_COMPOSE_PROJECT` | `penpot-local` | The `-p` compose project name |
| `PENPOT_COMPOSE_FILE` | `docker-compose.yaml` | The base compose file |
| `PENPOT_COMPOSE_OVERRIDE_FILES` | `docker-compose.local.yml` | Comma-separated `-f` overlays, applied after the base |
| `PENPOT_COMPOSE_ENV_FILE` | `.env.local` | `--env-file` |
| `PENPOT_EXPECTED_SERVICES` | `penpot-frontend,penpot-backend,penpot-exporter,penpot-postgres,penpot-valkey` | Comma-separated compose service names `reconcile()` checks |

This is deliberate: the plan this service was built against calls for the
exact same module to be installed on sirius later, pointed at whatever path
sirius checks the fork out to, without a code change — only different env
values.

## CAUTION: Docker bypasses UFW — published ports must bind loopback only

On any host that also runs a firewall (sirius, in particular), **Docker's
own `-p` port publishing inserts `iptables` DNAT rules into the `nat` table
that are evaluated before UFW's `filter`/`INPUT` rules ever run.** A
container published the ordinary way — `-p 9001:8080` — is reachable from
the entire internet, and `ufw status` keeps printing a locked-down ruleset
that looks correct while it is being bypassed entirely. This is not specific
to Penpot; it is true of every container Docker publishes on that host.

**Every published port in the compose override must bind explicitly to
`127.0.0.1`** — `127.0.0.1:9001:8080`, never the bare `9001:8080` form —
because Penpot is meant to sit behind httpsfront on loopback anyway, so the
loopback bind costs nothing and closes a real hole. This service does not
enforce that (it has no view into the compose YAML's content, only its exit
code), so it is stated here as a hard requirement on whoever edits
`docker-compose.local.yml`. sirius additionally runs a `DOCKER-USER`
default-deny chain as defense in depth, since the loopback bind is a
convention, not something Docker itself enforces.

## What this service manages, and what it does not

| | this service | the compose stack itself |
|---|---|---|
| start / stop / restart | `awm penpot start/stop/restart` → `docker compose up -d` / `down` | each container's own `restart:` policy handles a crash mid-run |
| health | polls `docker compose ps` container state (see `stack.reconcile`) | Docker's own healthchecks, where an image declares one |
| logs | `awm penpot logs` tails `docker compose logs` | Docker retains each container's stdout/stderr itself — nothing here re-captures it into a service-owned file |
| images | none — assumes they already exist, tagged in the override file | built by `projects/penpot/dev/docker/images/build.sh` |
| accounts / teams / files | none | Penpot's own Postgres |

## Registrations

One, and it carries no traffic:

| kind | name | prefix | what |
|---|---|---|---|
| `service` | `penpot` | `/svc/penpot` | the verbs and the supervisor |

`awm penpot url` answers `/penpot` — a path, not a URL, the same convention
Trilium's `trilium url` uses: it is on whatever origin the caller is already
on, behind the same session. Serving that path is a separate httpsfront
change, not this service.

## Who may call what

`status` and `url` are open to any signed-in caller; `start`, `stop`,
`restart` and `logs` are refused for any caller that arrived through an
edge — the same `_operator_only` split Trilium uses, and for the same
reason: the stack is shared, so a write verb is one person's action against
everyone's open editor session. See `hub_adapter._operator_only` for the
discriminator (an edge always stamps `X-Awm-As`; the host's own CLI never
does).

## Install

```
./install.sh
```

`awm/gateway/install.sh` runs this on every deploy. It editable-installs the
Python package into the `awm` env and bakes the `.runtime-env` sidecar every
awm service uses to respawn under systemd's minimal PATH. It does **not**
install Docker, and does **not** build or pull any Penpot image — both are
out of scope here. A missing `docker` binary is a warning at install time,
not a failure: the service still registers so `awm penpot status` can report
why the stack cannot come up, rather than the whole `awm/gateway/install.sh`
loop aborting on a node that doesn't run this service yet.

## Verify

```
awm services list | grep penpot
awm penpot status
```

`status` reports `stack_state` (`stopped` / `unhealthy` / `healthy`) and,
from the console (not through an edge), which containers are missing or
unhealthy and where the compose directory is. The check that actually
matters is not this: **open `/penpot` in a browser once httpsfront wires it
up, sign in or register, and confirm a board opens and stays live** — a
`status` of `healthy` proves the containers are up, not that the frontend's
websocket collab session actually connects.

# Penpot feature work: which loop

## Purpose & Contents

This file routes a future Penpot feature request down the right implementation
path before any code gets written. It holds the one decision that matters (core
change vs. plugin), the concrete steps for each path, and the failure modes
that bite regardless of which path you pick. It does not explain Penpot's
plugin API, the compose stack's architecture, or how any individual service
works — those live in `awm/services/penpot/INSTALL.md`,
`awm/services/penpot-plugins/INSTALL.md`, `awm/services/penpot-view/INSTALL.md`,
and the module docstrings each one points at.

## The decision

Ask one question: **does the feature need something the plugin sandbox cannot
reach?** The plugin sandbox is a browser-side script with no server-side
hooks and no raw storage access — its only door to the outside world is
Penpot's own plugin API surface (`penpot.*`).

A feature forces the core-change path if it needs any of:

- Access to a secret (an API key, a service-account password) that must not
  ship to the browser.
- A call to another awm service, or to anything off Penpot's own plugin API.
- Direct access to Penpot's Postgres database or its raw asset storage.
- Automation with no plugin-API hook to trigger it — no relevant entry in
  `penpot.on`'s event list, and no toolbar/UI action that can drive it.

If none of those apply, it is a plugin. When in doubt, prototype it as a
plugin first: the turnaround cost of being wrong is seconds, not minutes.

## The plugin loop

1. Copy `awm/services/penpot-plugins/local/_template/` to
   `awm/services/penpot-plugins/local/<name>/`.
2. Fill in `manifest.json` and `plugin.ts` following the template's own
   comments, then build `plugin.ts` to `plugin.js`.
3. Install it from `/penpot-plugins/<name>/manifest.json` in Penpot's Plugin
   Manager.

Full mechanics — the mount, the build command, permissions, the two-way UI
message loop, the `penpot-view-refresh` plugin as a worked example — are in
`awm/services/penpot-plugins/INSTALL.md`. No restart is needed for an edit to
an existing plugin file: the mount reads `local/` off disk per request. A
**new** plugin folder needs the `penpot-plugins` service already running.
**Turnaround: seconds** — edit, rebuild `plugin.js`, reload the page.

## The core-change loop

1. Edit the Penpot fork (`projects/penpot/<scope>/`) under the affected
   module (`frontend`, `backend`, `exporter`) or under `common/`.
2. Run `awm/services/penpot/scripts/promote-local.sh [module ...]`. With no
   arguments it checks all three modules, rebuilds only the ones whose source
   (module + `common/`) changed since their last recorded build, restarts
   only those compose services, and health-checks before declaring success.
3. Confirm the reported result — it exits nonzero on drift or a failed health
   check rather than leaving that to you to notice.

See `awm/services/penpot/INSTALL.md` for the service that supervises the
stack around this (start/stop/status/logs) and the compose configuration
surface. **Turnaround: minutes** — a full `manage.sh build-<module>-bundle`
plus a `docker build`, not a hot reload.

Pick correctly the first time: shipping a plugin-shaped feature as a core
change costs minutes per iteration where seconds were available; discovering
mid-build that a "plugin" actually needed a secret or another service means
restarting from the core-change loop anyway.

## What bites either way

- **CAUTION: `PENPOT_PUBLIC_URI` must name the mount exactly, or every route
  renders the not-found page.** Penpot's client compares
  `location.origin + location.pathname` against its configured public URI by
  exact string equality before it routes anything (`on-navigate` in
  `frontend/src/app/main/ui/routes.cljs`). The value is read at *runtime* from
  the `penpotPublicURI` JS global, which `files/nginx-entrypoint.sh` writes
  into `js/config.js` from the environment variable. So Penpot serves under a
  path prefix with no rebuild and no source change: set
  `PENPOT_PUBLIC_URI=<edge origin>/penpot` and strip the prefix at the edge.
  Omit the trailing slash from the variable — the backend concatenates it raw
  into email templates — and keep it on every link, because the comparison
  normalises the configured value to end in one.

  The failure is worth recognising by sight: a mismatch renders Penpot's 404
  page, **which embeds a login dialog**. It reads as an expired session and is
  not one. An earlier pass here diagnosed it as "the router cannot parse a
  sub-path" and gave Penpot the origin root to work around it. That was wrong,
  and the workaround cost the edge its landing page.

- **CAUTION: the exporter needs its own origin, and giving it the public one
  breaks every export.** The exporter drives a headless browser at
  `<internal-uri>/render.html`, which loads the same `js/config.js`. Point it
  at a frontend whose config names the *public* origin and that browser has no
  session on the edge, so the page never reaches network idle and every export
  dies on `ResourceRequest timed out`. Ungating the render page instead is
  worse — it is reachable unauthenticated.

  The fork already carries the split. Set the exporter's `PENPOT_INTERNAL_URI`
  to a **second frontend container with `PENPOT_PUBLIC_URI` unset**, whose
  config then falls back to `location.origin` and whose own location check
  passes on the internal address. `replace-internal-uris`
  (`exporter/src/app/renderer/svg.cljs`) rewrites that origin back to the
  public one in the emitted SVG, so nothing internal reaches a caller. Costs
  one nginx container. Never give that container `PENPOT_PUBLIC_URI`.

- **WARNING: anything Penpot fetches server-side must clear its SSRF guard,
  and loopback is not reachable from its containers regardless.** Any
  feature that has Penpot's backend fetch a URL (`uploadMediaUrl` and
  anything shaped like it) hits `backend/src/app/util/ssrf.clj`, which
  rejects loopback, link-local, site-local and RFC1918 addresses outright
  with `:ssrf-blocked-target` — a reachable private address is refused
  regardless. The awm gateway itself binds loopback only, which is a second,
  independent reason a bare `127.0.0.1` URL never works here. The supported
  escape hatch is `PENPOT_SSRF_ALLOWED_HOSTS`, an exact case-insensitive host
  allow-list — add the real hostname there, don't widen the guard.

  Clearing both still leaves a third wall, and it has no answer yet: that
  fetch carries no awm session, so any awm-served URL behind the edge's gate
  answers it 401. The plugin sandbox cannot fetch it in the backend's place —
  `plugins/libs/plugins-runtime/src/lib/create-sandbox.ts` forces
  `credentials: 'omit'` and exposes no binary response reader. This is what
  stops `penpot-view-refresh` working; the full trace and the two candidate
  routes out are in `awm/services/penpot-plugins/INSTALL.md`. Design any new
  server-side-fetch feature around this before building it.

- **WARNING: `enable-demo-users` and `disable-secure-session-cookies` must
  never reach a public deployment.** They are in the local compose file's
  `PENPOT_FLAGS` because local verification runs over plain HTTP and needs an
  account to log in with. Demo users are self-service throwaway accounts, so
  shipping that flag makes the instance open to anyone who can reach it, and
  the local database holds nothing but demo profiles today. Provision real
  accounts instead with `docker exec penpot-backend python3 manage.py
  create-profile`, which drives the backend's PREPL. Enabling
  `enable-prepl-server` for that opens no network port: `prepl-host` defaults
  to `localhost` in `backend/src/app/main.clj`, so the socket is
  container-local. Never set `PENPOT_PREPL_HOST`, and never publish 6062-6064.

- **CAUTION: a rebuilt image is not a fact about the running container.**
  `promote-local.sh` re-verifies the actually-running image against what it
  just built (or, on a no-op run, against the last recorded build) rather
  than trusting `docker compose up`'s exit code, because that exit code is 0
  even for a container that starts and immediately crash-loops. Trust its
  drift check, not a green build log, when confirming a change shipped.

- **WARNING: never publish a container port bare on sirius, or anywhere
  Docker shares a host with a firewall.** Docker's own `-p` port publishing
  inserts `iptables` DNAT rules evaluated before UFW's filter rules run, so
  `-p 9001:8080` is a public exposure on the open internet while `ufw status`
  keeps printing a locked-down ruleset. Always write `127.0.0.1:9001:8080`.
  Leave the `DOCKER-USER` default-deny chain installed there as a second
  layer — it is defense in depth for exactly this convention, not a
  redundant rule to clean up.

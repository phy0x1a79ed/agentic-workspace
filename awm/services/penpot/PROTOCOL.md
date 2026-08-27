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

- **CAUTION: Penpot must own the origin root, or its router silently shows
  the login screen forever.** The client-side router slices
  `location.pathname` against a build-time path prefix that ships empty.
  Served at a sub-path like `/penpot`, the app renders its login screen on
  every route even with a valid session — the shell paints, every asset
  returns 200, and nothing in the network tab looks wrong. Use
  `awm.httpsfront.penpot.owns(..., at_root=True)`, which means one edge fronts
  Penpot or Trilium, never both, until one gets a real URL base.

- **CAUTION: `PENPOT_PUBLIC_URI` is load-bearing in two directions that
  conflict behind an authenticating edge.** The browser bakes it into
  `js/config.js` as the API origin it calls, so it must be the edge's public
  origin. But the exporter container's headless browser also loads that same
  `config.js` when rendering `<internal-uri>/render.html`, so that origin
  must simultaneously be reachable and un-gated from inside the exporter
  container. A gated edge here fails every export with
  `ResourceRequest timed out`. There is no code fix — resolve it as a
  deployment decision (which origin, how it's gated) before shipping either
  loop's change.

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

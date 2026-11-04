# Trilium fork: move to mizar and first release (R115 item 4)

## Purpose & Contents

This draft holds the T27 steps that move the Trilium fork from altair to mizar, and that publish its first release tarball for awm's `server.pin`. The awm side is built on `feat/redux-cutover`: `awm/services/trilium/server.pin` and its `pin` mode in `install.sh`. The steps here touch the fork and the hosts, which T18 could not reach.

## Steps

1. On altair, check that `projects/trilium` has a clean `dev` and `release` and that `release` is pushed to its bare repo. Write down `git -C projects/trilium/release describe --tags`.
2. Move the fork's bare repo to mizar with every ref, as `cloud`'s `canonical_repos` role moves awm (`take-refs.sh`). Then create the `dev` and `release` worktrees under `projects/trilium/` on mizar. Do not run `bootstrap-fork.sh`: it clones upstream again.
   - Check: `git -C projects/trilium/release describe --tags` on mizar matches step 1.
3. On mizar, build the server once on `release` (`pnpm install --frozen-lockfile`, then `pnpm server:build`). Then package `apps/server/dist` with the Node runtime that `.nvmrc` names, as upstream's server tarball does: one top-level folder holding `main.cjs` and `node/bin/node`. Name it `TriliumNotes-Server-<describe>-linux-x64.tar.xz`. Make this the fork's release script, so later releases follow the same route.
4. Publish the tarball at a url deneb can fetch with `curl`. **Open choice for the coordinator:** a GitHub release of the fork, or a file served from mizar over the deneb edge.
5. In awm, set `server.pin` `version`, `url` and `sha256` (`sha256sum` of the tarball). Commit, then deploy awm.
   - Check: on deneb, `awm/services/trilium/tarball-stamp` reads `fork <version> sha256:<hash>`, and `awm trilium status` reports the fork as the source.
6. Try a fork change on mizar's dev server against a copy of a vault snapshot, never the live vault (`pnpm server:start` in `projects/trilium/dev`).
7. Once deneb serves the pin, a T23 cleanup task deletes the build and upstream-tarball paths from `install.sh`, together with `bootstrap-fork.sh` and `ship-bundle.sh`.

## Gotchas

- `install.sh` picks build mode on any node that has `projects/trilium/release/.git`. On mizar the pin wins, because `auto` checks the pin first. Keep that order.
- The upstream tarball has no slice mask, and a fork tarball does. Only the `fork ` stamp tells them apart, so never hand-copy a tarball into `server/` without the stamp.

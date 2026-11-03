# Install and bootstrap

## Purpose & Contents

This doc holds how awm reaches a node and how awm is built: the language of the core, the build, the plugins, and the environments that test a fresh install. Thread 1h produces it.

Which node holds which role belongs in `fleet.md`. How a deploy moves through the branches belongs in `routines.md`.

## Requirements

- **R112.** The core of awm is rewritten in Rust. Tony: "rust for core awm, yes" (2026-11-02).
- **R113.** mizar holds the Rust build container. Rust builds and checks run in that container, never in a toolchain on the host. The container keeps a cargo cache in a volume, so a rebuild stays fast. Tony: "the container can use a cargo cache too" and "mizar is home base. it has the build contianer" (2026-11-02).
- **R114.** awm's remote is https://github.com/phy0x1a79ed/agentic-workspace. Tony: "this is awm's remote https://github.com/phy0x1a79ed/agentic-workspace" (2026-11-02).

## Gap from today

Surveyed read only on 2026-11-02, from the `feat/redux` source and the deployed tree on mizar.

- **All of awm is Python.** The gateway is about 14.5k lines, the shared libraries 8.4k, the services 107k and the tests about 83k. On mizar `awm --help` takes 1.4 s, and the `awm` env holds 2.7 GB, torch alone 776 MB. Neither mizar nor altair has `cargo`.
- **A bare box takes about 15 steps to become a node.** `awm/gateway/setup.sh` and `awm/gateway/install.sh` make the Python env, the installs, `gateway init` and the command wrappers. Every other step is manual: `.awm/env`, `enabled.json`, nodejs, the systemd unit, the certificate, peer keys, `known_hosts`, the Claude login, hooks and the secrets of each service.
- **A fresh node starts every service** unless an explicit `enabled.json` limits it.
- **Install still needs sudo** for the `/usr/local/bin` links, apt packages and linger. This breaks `fleet.md` R101.
- **The plugin contract is already blind to language and place.** A plugin is a folder with a `run.sh` that connects to the gateway. `AWM_SERVICE_ROOTS` (commit 041102fa) loads services from outside the repo, but no node sets it. All 44 services sit in `awm/services/`. tether is the nearest to an outside plugin: a pinned release with Rust binaries from GitHub.
- **R33 missing.** No plugin has its own refresh hook or version pin. `awm deploy` restarts the whole gateway. Every Python service shares the one `awm` env.
- **No test environment exists.** awm has no Dockerfile, no CI, no cloud-agent setup and no fake peer. Rootless Docker works on mizar and in capella's WSL.
- **mizar has two miniforge installs.** The services run from `~/miniforge3`. `.mcp.json` points at `~/lib/miniforge3`.

# Install and bootstrap

## Purpose & Contents

This doc holds how awm reaches a node and how awm is built: the language of the core, the build, the plugins, and the environments that test a fresh install. Thread 1h produces it.

Which node holds which role belongs in `fleet.md`. How a deploy moves through the branches belongs in `routines.md`.

## Requirements

- **R112.** The core of awm is rewritten in Rust. The core is the bootstrap binary, the CLI, the MCP proxy and the gateway with its plugin loader. Every service is a plugin (R115). Tony: "rust for core awm, yes" and "this is correct." (2026-11-02).
- **R113.** mizar holds the Rust build container. Rust builds and checks run in that container, never in a toolchain on the host. The container keeps a cargo cache in a volume, so a rebuild stays fast. Tony: "the container can use a cargo cache too" and "mizar is home base. it has the build contianer" (2026-11-02).
- **R114.** awm's remote is https://github.com/phy0x1a79ed/agentic-workspace. Tony: "this is awm's remote https://github.com/phy0x1a79ed/agentic-workspace" (2026-11-02).
- **R115.** A plugin is a project of its own, and awm installs it straight from its git repository. Installing one needs no code in awm, such as a line that registers a service or a page. The plugin carries everything awm needs to load it. Tony: "by true plugins, I meant that the current system is not rigorous.", "each plugin should be its own project" and "plugins should be installable from a git repo directly and not require any additional code in awm itself, like registering services or pages." (2026-11-02).
  1. A page is a plugin too. Tony: "pages id argue are also plugins."
  2. One plugin may hold several services and pages. Tony: "a plugin can have a combination and multiple services and pages".
  3. A plugin has a version pin and its own refresh hook (`routines.md` R33). It runs on the host with its own env or in a container.
  4. The core plugins stay in the awm repository, each in its own folder. A plugin is a folder whose top level holds one manifest file that fills out the plugin contract. awm installs it from a git repository or from a folder alike. Tony: "we can also keep core plugins in awm repo itself. then installable is from a folder structure, maybe some file at top level that fills out contract" (2026-11-02).
- **R116.** Guards keep a plugin from reaching past its own parts. Tony: "your proposals are good" (2026-11-02).
  1. The manifest lists the plugin's services and pages and what they need, such as secrets, network and other tools. The gateway gives a plugin only what its manifest lists.
  2. Each install pins a commit and takes Tony's passkey (`queue.md` R64), and so does each move to a new commit. One passkey approves every plugin commit of one change together. Tony: "approved commits are fine if we can batch approve for a full change" (2026-11-02).
  3. Each plugin's pages get their own origin, so a page reaches only its own plugin's services.
  4. Tony's own plugins run on the host. A third-party plugin runs in a container at untrusted, as an isolated session does (`fleet.md` R109).
  5. A namespace records which plugin owns a name. It never appears in a call: a tool keeps its flat name, such as `journal`. The manifest claims the tool names the plugin serves, and the gateway keeps one table from each name to its owner. An install that claims a taken name is refused, and Tony settles the clash by renaming one in the install config. Names that gate trust, such as `2fa` and the approver, belong only to core plugins. Page origins, secret grants and the audit log use the owner's name. Tony: "we dont want namespace = tools and we dont want to have to do \"awm namespace/tool\"" and "this works" (2026-11-02).
- **R117.** A plugin depends on tools of other plugins, never on their code. Tony: "ok, but the install contract needs to name where these tools come from. the tool name is changeable in the contract so is not a good source of truth" (2026-11-02).
  1. A plugin calls another plugin only through the gateway, as an agent does. No plugin imports another plugin's code.
  2. Each tool declares a permanent UUID in its provider's manifest. The name may change, the UUID never does. A dependent names each tool it calls by UUID and the major version it needs. Tony: "then I think each tool should also declare a permanent uuid. the name can change, but dependent tools resolve and compile at isntall time to the correct name" (2026-11-02).
  3. At install the gateway resolves each UUID to the tool's current public name, on this node or on a peer, and compiles that name into the dependent. A rename in the install config (R116 item 5) recompiles the dependents in the same change. A fork keeps its tools' UUIDs, so its dependents resolve to the fork unchanged. Two installed plugins that serve one UUID clash, and the install is refused. A missing UUID refuses the install and names the UUID.
  4. A breaking change to a tool raises its major version. The provider and the plugins that call it update in one batch-approved change (R116 item 2).
  5. Shared code is a library that each plugin pins in its own env, not a plugin.
  6. Plugins start together, in no order. A call to a tool that is not up yet waits briefly, then fails with a clear error.

## Gap from today

Surveyed read only on 2026-11-02, from the `feat/redux` source and the deployed tree on mizar.

- **All of awm is Python.** The gateway is about 14.5k lines, the shared libraries 8.4k, the services 107k and the tests about 83k. On mizar `awm --help` takes 1.4 s, and the `awm` env holds 2.7 GB, torch alone 776 MB. Neither mizar nor altair has `cargo`.
- **A bare box takes about 15 steps to become a node.** `awm/gateway/setup.sh` and `awm/gateway/install.sh` make the Python env, the installs, `gateway init` and the command wrappers. Every other step is manual: `.awm/env`, `enabled.json`, nodejs, the systemd unit, the certificate, peer keys, `known_hosts`, the Claude login, hooks and the secrets of each service.
- **A fresh node starts every service** unless an explicit `enabled.json` limits it.
- **Install still needs sudo** for the `/usr/local/bin` links, apt packages and linger. This breaks `fleet.md` R101.
- **The plugin contract is already blind to language and place.** A plugin is a folder with a `run.sh` that connects to the gateway. `AWM_SERVICE_ROOTS` (commit 041102fa) loads services from outside the repo, but no node sets it. All 44 services sit in `awm/services/`. tether is the nearest to an outside plugin: a pinned release with Rust binaries from GitHub.
- **R115 partial.** The gateway discovers services from `awm/services/` and pages from a separate folder of built bundles (`AWM_PAGES_DIR`, `awm/gateway/awm/gateway/server.py` step 2b). Nothing installs either from a plugin's own repository, and one plugin cannot ship a service and its page together.
- **R33 missing.** No plugin has its own refresh hook or version pin. `awm deploy` restarts the whole gateway. Every Python service shares the one `awm` env.
- **No test environment exists.** awm has no Dockerfile, no CI, no cloud-agent setup and no fake peer. Rootless Docker works on mizar and in capella's WSL.
- **mizar has two miniforge installs.** The services run from `~/miniforge3`. `.mcp.json` points at `~/lib/miniforge3`.

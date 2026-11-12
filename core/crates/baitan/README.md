# baitan (摆摊)

## Purpose & Contents

baitan is a compute market. Callers post offer sets, providers bid, and the market awards each set to the best bid. This file covers how the market is deployed and how to add a provider. The design and its reasons are in `docs/dex-poc/DECISIONS.md`. The wire flow and the job kinds are in `docs/dex-poc/JOBS.md`. Run `baitan --help` for the roles and verbs.

## Topology

- One market runs, in the awm `baitan` plugin on mizar. The cloud inventory places it (`awm_plugins_homed` in `homelab/inventory/group_vars/all/plugins.yml`). A second market splits the providers, so keep the plugin on one node.
- Agents reach the market through the awm `baitan` tool from any node. The shell uses `baitan <verb>` on mizar, or sets `BAITAN_CONNECT` to a dial command elsewhere.
- The market dials every provider. `~/.baitan/baitan.toml` on mizar holds `links`, one shell command per link. The command must start a provider or front that speaks JSON lines on stdio.
- Each awm node runs `baitan provider` from `$AWM_DIR/bin/baitan`, configured by `~/.baitan/provider.toml` on that node.
- Each Slurm cluster runs `baitan front` from `~/.local/bin/baitan`, configured by `~/.baitan/front.toml`. The market reaches the clusters through altair, which holds their ssh masters.

**CAUTION** altair cannot ssh to mizar. Callers on altair, such as the metasmith executor, reach the market socket through an `ssh -R` forward that mizar opens. That forward does not survive a reboot.

## Add a node provider

1. Write `~/.baitan/provider.toml` on the node with `id`, `slots` and `props.name`. Add `routes` to the providers it can fetch from with rsync.
2. Add `ssh -o BatchMode=yes <node> <awm_dir>/bin/baitan provider` to `links` in mizar's `~/.baitan/baitan.toml`.
3. Restart the awm `baitan` plugin on mizar.

## Add a Slurm cluster

1. Build the cluster binary on altair: `cargo build --release --target x86_64-unknown-linux-musl -p awm-plugin-baitan` in the awm `core/` tree. Leave the `awm` feature off.
2. Copy it to `~/.local/bin/baitan` on the cluster.
3. Write `~/.baitan/front.toml` with one `[[cluster]]` holding `[cluster.provider]` and `[cluster.slurm]`. Set `account`, `walltime`, `scratch` and `bin_dir`.
4. Add `ssh -o BatchMode=yes altair ssh -o BatchMode=yes <cluster> .local/bin/baitan front` to `links`.
5. Restart the awm `baitan` plugin on mizar.

**CAUTION** Put the provider's `state_dir` on scratch. Compute nodes cannot write home on these clusters, and each job writes its `exit` file into its work dir under `state_dir`.

**CAUTION** Set `whole_node = true` in `[cluster.slurm]` on a cluster that allocates whole nodes and refuses `--mem`, such as trillium.

**WARNING** Never retry an ssh connect to a cluster in a loop. fir locks the account after 10 failed Duo prompts. Open a master once with the awm `ssh` tool.

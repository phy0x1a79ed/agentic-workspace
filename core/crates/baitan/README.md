# baitan (摆摊)

## Purpose & Contents

baitan is a compute market. Callers post offer sets, providers bid, and the market awards each set to the best bid. This file covers how the market is deployed and how to add a provider. The design and its reasons are in `docs/dex-poc/DECISIONS.md`. The wire flow and the job kinds are in `docs/dex-poc/JOBS.md`. Run `baitan --help` for the roles and verbs.

## Topology

- One market runs, in the awm `baitan` plugin on mizar. The cloud inventory places it (`awm_plugins_homed` in `homelab/inventory/group_vars/all/plugins.yml`). A second market splits the providers, so keep the plugin on one node.
- Agents reach the market through the awm `baitan` tool from any node. The shell uses `baitan <verb>` on mizar, or sets `BAITAN_CONNECT` to a dial command elsewhere.
- The market dials every provider. `~/.baitan/baitan.toml` on mizar holds `links`, one shell command per link. The command must start a provider that speaks JSON lines on stdio.
- Each awm node runs `baitan provider` from `$AWM_DIR/bin/baitan`, configured by `~/.baitan/provider.toml` on that node.
- The provider on altair also hosts every Slurm cluster, one provider per `[[cluster]]` in its `provider.toml`. altair holds the cluster ssh masters. Its Slurm backend reaches each cluster only by one-shot `ssh -o BatchMode=yes` calls over those masters. No baitan process runs on a cluster, only the job scripts on its compute nodes.

**CAUTION** altair cannot ssh to mizar. Callers on altair, such as the metasmith executor, reach the market socket through an `ssh -R` forward that mizar opens. That forward does not survive a reboot.

## Add a node provider

1. Write `~/.baitan/provider.toml` on the node with `id`, `slots` and `props.name`. Add `routes` to the providers it can fetch from with rsync.
2. Add `ssh -o BatchMode=yes <node> <awm_dir>/bin/baitan provider` to `links` in mizar's `~/.baitan/baitan.toml`.
3. Restart the awm `baitan` plugin on mizar.

## Add a Slurm cluster

1. Open the cluster's ssh master on altair with the awm `ssh` tool.
2. Add a `[[cluster]]` to altair's `~/.baitan/provider.toml`. `docs/dex-poc/altair-provider.toml.example` shows the shape.
3. Restart the awm `baitan` plugin on mizar, so the market redials `ssh altair baitan provider`.

**CAUTION** Set `work_dir` and `scratch` to paths on the cluster's scratch. Compute nodes cannot write home on these clusters, and each job writes its `exit` file into its work dir. `state_dir` and `jobs.json` stay on altair.

**CAUTION** Set `whole_node = true` in `[cluster.slurm]` on a cluster that allocates whole nodes and refuses `--mem`, such as trillium.

**CAUTION** A dead master shows as a failed call in the provider log and as `error` in a job's sign of life. The backend retries at the next gate and never opens a login. Reopen the master with the awm `ssh` tool.

**WARNING** Never retry an ssh connect to a cluster in a loop. fir locks the account after 10 failed Duo prompts.

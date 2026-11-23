# outsource

## Purpose & Contents

outsource is a compute market. Callers post offer sets, providers bid, and the market awards each set to the best bid. This file covers how the market is deployed and how to add a provider. The design and its reasons are in `docs/dex-poc/DECISIONS.md`. The wire flow and the job kinds are in `docs/outsource/JOBS.md`. Run `outsource --help` for the roles and verbs.

## Topology

- Every agent host runs the awm `outsource` plugin. The plugin is the node's entry to the one market and the host of the node's providers. Agents post at their own node's entry.
- The entry reaches its own providers in process. It reaches the providers of every node it holds a user edge to in `edges.yml`, by `ssh <node> <awm_dir>/bin/outsource attach`. A node whose service is down fails the attach, and the entry redials up to once a minute.
- The node's providers come from its `~/.outsource/provider.toml`. They listen on `~/.outsource/provider.sock` for the entries that attach.
- The providers on altair include every Slurm cluster, one provider per `[[cluster]]` in its `provider.toml`. altair holds the cluster ssh masters. Its Slurm backend reaches each cluster only by one-shot `ssh -o BatchMode=yes` calls over those masters. No outsource process runs on a cluster, only the job scripts on its compute nodes.
- The standalone `outsource serve` takes the attach commands from `links` in `~/.outsource/outsource.toml` in place of the edge list.

## Add a node provider

1. Write `~/.outsource/provider.toml` on the node with `id`, `slots` and `props.name`. Add `routes` to the providers it can fetch from with rsync.
2. Enable the awm `outsource` plugin on the node.

## Add a Slurm cluster

1. Open the cluster's ssh master on altair with the awm `ssh` tool.
2. Add a `[[cluster]]` to altair's `~/.outsource/provider.toml`. `docs/outsource/altair-provider.toml.example` shows the shape.
3. Restart the awm `outsource` plugin on altair.

**CAUTION** Set `work_dir` and `scratch` to paths on the cluster's scratch. Compute nodes cannot write home on these clusters, and each job writes its `exit` file into its work dir. `state_dir` and `jobs.json` stay on altair.

**CAUTION** Set `whole_node = true` in `[cluster.slurm]` on a cluster that allocates whole nodes and refuses `--mem`, such as trillium.

**CAUTION** A dead master shows as a failed call in the provider log and as `error` in a job's sign of life. The backend retries at the next gate and never opens a login. Reopen the master with the awm `ssh` tool.

**WARNING** Never retry an ssh connect to a cluster in a loop. fir locks the account after 10 failed Duo prompts.

# refresh-all: every awm node on the current release

> Is Shogun the only one? We should aim to refresh all.

The fleet deploy refreshes the nodes in the install config's `[fleet] nodes`. The cloud project renders that list from `homelab/inventory/group_vars/all/deploy.yml` (`awm_fleet_nodes`, `awm_fleet_push`, `awm_fleet_sleepers`). The current list is mizar, alcor, capella, lyra, deneb, altair.

## State on 2026-11-06, release b1e85c50

| Node | Release | Why |
|---|---|---|
| mizar, alcor, altair, deneb | b1e85c50 | current |
| shogun | deb4d72d | It is not in `awm_fleet_nodes`. Cloud's `awm_node` role (`moves.yml --tags shogun_awm`) installed it once at pass 1 (journal 523a8e22). No deploy has reached it since. shogun has no Claude Code daemon, and its browser and operations services are stopped. |
| capella | 0f19ebc6 | A sleeper: the deploy skips it while it is asleep. Woken at 10:10 MST with `wake`. After boot, its `awm` user unit is disabled, the gateway does not run and no `awm-claude-daemon` unit exists. Two earlier batches failed on capella with `daemon_down`. |
| lyra | none | awm is not installed. It waits on Tony's UAC click (journal eb625841). |
| fir, sockeye | n/a | Stations, not awm nodes. |

## Tony's answer (2026-11-06)

> shogun doesnt need warm claude daemon, and should be off most of the time, but for the next week we keep it on for patches. We spawn the claude daemon as needed on shogun.

> capella can start awm at boot

## Design

- shogun joins the deploy fleet. It has no warm Claude Code daemon, so its refresh must not depend on one. The refresh spawns the daemon when it needs one. shogun stays on through 2026-11-13 for patches, then is off most of the time, so the deploy treats it as a sleeper.
- capella enables its `awm` user unit, so awm starts at boot.

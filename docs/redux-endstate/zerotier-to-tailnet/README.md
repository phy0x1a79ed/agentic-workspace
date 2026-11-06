# zerotier-to-tailnet

## What Tony said (2026-11-06)

> cloud science on altair (and possibly other nodes) still point to old zerotier IP, so we have sign in to "10.74.81.84" for entering claude science and it just breaks. Need to move services to new tailscale system.

## Design

Every service a node publishes, and every route one node takes to another, uses the node's tailnet name (`<node>.tail080e54.ts.net`). No config, env file or ssh config keeps a ZeroTier `10.74.81.x` address. The homelab inventory in the cloud repo holds each change, so a play reproduces it.

## Done (portals-ops, cloud 3df576f, on cloud dev)

- Portals answer 401 with valid certs on alcor, altair, deneb and mizar.
- claude-science on altair: https://altair.tail080e54.ts.net:12201/ after sign-in at https://altair.tail080e54.ts.net/. Its fronts moved to 12211/12212. tailscale serve publishes :12201/:12202. The daemon's allowed origins hold the tailnet origin.
- altair AWM_EDGE_URL is the tailnet name. Gateway restarted.
- alcor and mizar host_vars and deneb awm-client.conf use tailnet names in the repo.
- Trilium: the old Ops password notes and old Ops deleted. Links moved under awmSwarmOps.

## Left

- Apply the repo change on alcor and deneb: play, then gateway restart.
- Apply on mizar. A gateway restart on mizar stops its sessions.
- capella `.awm/env` (10.74.81.110). ssh configs on altair and capella.
- shogun's inventory holds altair's hand changes (443 serve, 12201→12211 and 12202→12212 serve, FRONT_PORT/SANDBOX ports, EXTRA_ORIGINS, AWM_EDGE_URL).
- claude-science on capella and alcor: check their origins.
- capella and lyra portals wait on the Windows-side Tailscale (R95/R96). lyra is unreachable.

## mizar's restart (2026-11-06)

> do we need the gateway restart for anything? -> can we make the gateway restart more seamless? what is it for anyways?

mizar's AWM_EDGE_URL was already a tailnet address (`https://100.125.95.5:12100`), not ZeroTier. The change to `https://mizar.tail080e54.ts.net` only makes the links mizar mints use the name its certificate covers. Nothing breaks without it, so the restart waits. It applies with the next gateway start. Making that restart seamless is the topic env-reload.

# node-time: every node shows Tony's local time

> Also, can you check the times across nodes? The time should be 10 a.m.

## State on 2026-11-06 at 17:08 UTC

Every reachable clock agrees with UTC to within 4 seconds, and NTP is synchronised on each one. The difference is the time zone only.

| Node | Time zone | Local time shown | tzdata |
|---|---|---|---|
| mizar | America/Vancouver | 10:08 MST | 2026e |
| alcor | America/Vancouver | 10:08 MST | |
| capella | America/Creston | 10:11 MST | |
| altair | America/Los_Angeles | 09:08 PST | 2026e |
| shogun | Etc/UTC | 17:08 UTC | 2025b. Too old: America/Vancouver still gives PST there |
| deneb | Asia/Shanghai | 01:08 CST, next day | 2026e |
| lyra | unreachable | | |

On tzdata 2026e, America/Vancouver is UTC−7 all year, so it shows 10 a.m.

Timers that a zone change moves:
- altair: `arbutus-tfstate-offsite.timer` and `kb-papers-nightly.timer` name no zone and follow the system zone. Three more timers name America/Los_Angeles and do not move.
- deneb: `awm-trilium-copy.timer` (03:10, no zone) moves from 19:10 UTC to 10:10 UTC.

## Tony's answer (2026-11-06)

> 2 yes -> this includes altair and basically every machine on arbutus (time zone correction)

## Design

The cloud project's Ansible baseline sets America/Vancouver and installs current tzdata on every node it manages. That covers the awm nodes and the Arbutus machines (altair, shogun, showtime and the workers).

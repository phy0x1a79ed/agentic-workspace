# memory

## Purpose & Contents

This file holds the memory bound on search: what the 100 MB covers, and how the corpus relates to RAM.

## As built

- Measured on altair's journals (367a5d0d). The search process holds 825 MB resident at 1x and 1.9 GB at 10x.
- Warm latency relies on the OS page cache holding the index. With the page cache evicted, ranked queries ran past 120 s.

## Decisions

- 2026-11-05: Memory is a soft target, not a hard constraint. Search runs as a service on every node, so it holds little memory steadily. Vectors held in memory are out. Short spikes while a query runs are acceptable within reason: a spike of several GB that could crash a busy box is not. Even 1 GB held permanently is too much, also on a node with RAM to spare. The 100 MB figure is a rough scale, not a limit. Tony: "This is a soft target. I'm just throwing numbers. All I mean is that in order for search to run as a service, it should not hold a large amount of constant ram. This obviously means that storing vectors in memory is off the table, but instantaneous use and spikes is acceptable, within reason." and "even though altair has plenty of ram, permanently consuming a large part of that (something like 1gb, even) is not desirable for a core service that every agentic node will need".

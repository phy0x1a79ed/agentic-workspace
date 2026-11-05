# rollout

## Purpose & Contents

This file holds how the LanceDB search engine reaches the fleet: the merge route, the swap on each node, and what the build must report.

## Decisions

- 2026-11-05: Hot-swap the new search in. The fleet cutover runs on awm dev 2c328c82 with the current search and does not wait for this branch. When the branch is done, it merges into dev through redux-build/dev. `routine deploy` then swaps it in on each node: a search restart, a journal restart if journal code changed, and a gateway refresh, with no other step. Relayed by Swarm triage, Tony: "For search, possible to just hotswap it in once new version done right? If so, lets do that".
- Any index format change migrates or rebuilds itself when search starts, with no manual step.
- Report the restart set and the rebuild time per node to redux-build/dev and to Swarm triage (job 8162a704) before the merge.
- The no-capella and no-mira rules hold.

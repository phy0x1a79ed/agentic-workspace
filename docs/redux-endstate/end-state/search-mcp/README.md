# search-mcp: every node runs the new search service and the Rust MCP

> one thing we should review is that all nodes are using the new search service and MCP.

The spec: `fleet.md` R136 (the new search replaces the old one) and R98.3 (a node's MCP list agrees with `/table`). The Rust `awm mcp` (`core/crates/awm-mcp`) replaces the Python `awm-mcp` from the conda env.

## State on 2026-11-06

| Node | Release | Root `.mcp.json` | search service | Sources |
|---|---|---|---|---|
| mizar | b1e85c50 | Rust `awm mcp` | ready | awm, journals, kb, library, transcripts |
| alcor | b1e85c50 | Rust | ready | same |
| altair | b1e85c50 | Rust | ready | same |
| shogun | **deb4d72d** | Rust | ready | same |
| deneb | b1e85c50 | names `/home/tony/.local/bin/awm`, which deneb lacks | none, by role (board, relay, trilium) | none |
| capella | unverified, asleep | unverified | unverified | unverified |
| lyra | not installed | n/a | n/a | n/a |

## Gaps found

1. altair: five live sessions still ran the Python `awm-mcp`, loaded before the refresh. They were engcyc-raw-data 15a510ca, macos bu 747aac5c, Steven Chen's seat 31bd528d, msm-writing 9c8233d9 and Rokaiya's seat 0452781d. **Fixed:** `reflection mcp reconnect awm` on each, then the leftover Python processes were stopped. altair now has zero Python MCP processes. Each of the five has a Rust `awm mcp` child.
2. Stale scope worktrees carry a tracked `.mcp.json` that names the Python `awm-mcp`: 22 on mizar (old redux-* scopes, swarm-triage, deploy-compact-fix and others) and 77 on altair (old feat-*, fix-*, svc-* and seat-* scopes). A session started in one gets a dead MCP. This belongs to the scope cleanup under end-state.
3. shogun runs deb4d72d. That release's MCP lists only the tools served on shogun, so no door singleton (operations, social, 2fa and others) reaches shogun's sessions. shogun's browser and operations services are stopped.
4. capella was unreachable, and lyra has no awm yet. Neither can be checked today.
5. deneb's relay seats have no awm MCP. Their worktrees under `/var/lib/awm/projects/relay/` hold no `.mcp.json`. deneb's own `/opt/awm/.mcp.json` names a binary that does not exist there. The relay seats are due to move to mizar (journal f4ed07ad).

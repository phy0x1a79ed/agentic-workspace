# Port plan: guide

guide answers how to use awm. It is listed in `sources` with `default: false`, so a query reaches it only by naming it. It replaces search's `awm` tool-catalog source. search serves it in-process through awm-index (*Still unsure about*).

Contents, by section:
- **docs:** the workspace docs (`AGENTS.md`, `PROTOCOLS.md`, `FEDERATION.md`, `ARCHITECTURE.md`, `README.md`) and awm's `docs/`, from the deployed checkout.
- **plugins:** each installed plugin's `README.md`, `INSTALL.md` and shipped skills.
- **tools:** each tool's verbs, params and descriptions from the gateway's describe.

Each file is chunked by heading, and the hit names the file and heading.

Events, with no polling:
- A plugin install or update re-indexes that plugin's docs and help.
- A disable masks its rows, which stay stored but unanswered.
- An uninstall deletes its rows.
- An awm deploy is an install of awm itself, which re-indexes the workspace docs and `docs/`.

guide subscribes to the gateway's `awm/plugins` topic with `hub.subscribe("awm", "plugins", None, None)`.
- The payload is `{plugin, event, version, root}`.
- `event` is one of installed, updated, enabled, disabled, removed and refused.
- `version` is the install stamp, or the pin for a core plugin.

On each connect, and on search's start, guide reads `/hub/plugins` and reconciles each plugin's version against what it indexed, so a dropped socket loses nothing. plugin-redux/interview adds the topic on `feat/plugin-redux` and names the sha.

Tests:
- Port the suite.
- The redux's known-item replay (`replay.rs`): titles 100 of 100, sentences 99 of 99, known answers at least 4 of 6, and `grep` 100 % in `exact`.
- Federation: a stalled fake source and a down one, each within the timeout.
- Peer replays per source, including a source that omits `veiled`.
- guide: install, update, disable, re-enable and uninstall, each checked with a query.
- The engine benchmark measured LanceDB's vector recall@10 at 0.93 against exact search. Sweep nprobes (20, 40, 80) and `refine_factor` on the replay corpus. Pick the cheapest setting with recall ≥ 0.98 and cold p95 ≤ 0.11 s, and record it in the awm-index README. If no setting meets both, keep today's and report the curve to Tony.

# Handoff: the knowledge group's lands

## Purpose & Contents

This file hands the knowledge group's unlanded work to the session that merges it. It names the branch heads, the land order, the steps each land still needs, and what is deferred. Delete it once kb and scopes have landed and verified.

## Branch heads

- **mic:** `feat/port-knowledge-mic` at 9a3a569a. Ticket eae5c021aa0b is in land batch cffff968f16f, merged there as d93521a2.
- **kb and scopes:** `feat/port-knowledge` (awm). It holds awm-embed, awm-index, awm-kb-source, awm-kb-journals, awm-kb-search, awm-kb-guide, awm-kb-transcripts, `core/plugins/kb`, `core/plugins/scopes`, and the rename of the Python kb to `papers`.
- **cloud:** `feat/kb-journal-unit` at 6ff4b18, in `projects/cloud/kb-journal-unit`, pushed to the cloud origin.

## State of the code

- **CAUTION** No test of kb's sources or of scopes has run. Tony stopped the test runs. `cargo check -p awm-plugin-kb -p awm-plugin-scopes` passes. Clippy fails on a deny-level `absurd_extreme_comparisons` in awm-index.
- Run each crate's tests and clippy before the submit, and fix what fails. Each agent's open items are in its progress file under the job dir `/home/tony/.claude/jobs/2a70a96d/tmp/progress-{journals,search,transcripts,scopes}.md`. Copy what matters before that job is deleted.
- journals has no test target yet. `tests/journal/common.rs` is a harness skeleton. The 163 ported tests, the describe test and the write-order cases are still to write.
- The known-item replay gate (`replay.rs`) and the nprobes and `refine_factor` sweep are not done.
- `kb` has no `INSTALL.md` yet. Take the transcripts part from `git show 8ec603b7^:core/plugins/transcripts/INSTALL.md`.
- **CAUTION** `Journals::open` runs the first-start cutover, which embeds every post, before the service connects. On altair that is about 11 minutes. Check that the gateway's start timeout allows it, or move the cutover into `on_start`.
- mic's supersede test was flaky. 9bd62464 fixes it on `feat/port-knowledge`.

## Land order

1. **mic:** finish batch cffff968f16f. After the merge:
   - Run `<AWM_DIR>/bin/mic state check` on each node, and expect `lost: []`.
   - Move mizar's 17 MB `mic.log` aside.
   - Check the phone on capella.
2. **kb**, with awm-embed and awm-index, in one batch: `--restart kb`. journal, search, transcripts and the Python kb (now `papers`) stop in the same rollout.
3. **scopes**, after kb, because scope `search` asks kb's `journal_query`.

## Steps the kb land still needs

Before the restart, on each node:
1. **Singleton journal nodes:** run `homelab/journal_return.yml` from the cloud branch, then `site.yml --tags awm_config`, then the kb land.
   - **WARNING** It removes the `awm-journal` user and the unit. It keeps `/var/lib/awm/journal` as a root-owned rollback copy.
   - Interview grants the Ansible slot, one run at a time across cloud workers. Merge the latest cloud dev first, and send interview the `--check --diff`. Coordinate with comms, hpc-ssh-guard and trilium's telemetry-optin.
2. Run statecheck's inventory (`kb state inventory`) while the Python still runs.
3. Place the MiniLM model files at `<AWM_DIR>/models/` and check their sha256. Without them kb answers on keywords and embeds when the model loads.
4. Rename the clashing tags by hand: mizar and alcor hold `agent-comms` under different ids.
5. Review each node's `transcripts sweep {dry_run:true}` counts before the first sweep. The counts on 2026-11-08: mizar 3 sessions (7.9 MB), alcor 1 (0.9 MB), capella, altair and shogun 0. deneb does not run transcripts.

After the restart:
1. Stop and delete the `claude-archive-sessions` user units on capella and altair, after statecheck copied them.
2. Tighten the transcripts archive to 0600 files and 0700 directories.
3. Run the golden replays, `kb state check`, and journal's cutover gate on each node. Confirm that no Python process or timer of journal, search or transcripts remains.
4. Delete search's old `services/search/lance/` tables once kb answers. `.pre-rust/` keeps a copy.

## Steps the scopes land still needs

- Commit `awm-project.toml` to agentic-commons `main` in the same window. First confirm with `ssh altair` that altair runs the Rust scopes.

## Deferred

- The papers and library migration into kb's LanceDB on sockeye. Until then the Python `papers` plugin serves the tool `kb` and the sources `kb` and `library`.
- Transcript embedding. The index is full-text only.

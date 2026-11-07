# Port plan: awm-trust (grant, order, signer and trust helpers)

## Purpose & Contents

This file holds the approved port plan for awm-trust (grant, order, signer and trust helpers), from the ops group's plan (task CWPYAtyb). Tony approved it on 2026-11-06. It is the plan as approved: later changes go in the port commits and the journal.

## Rules common to every ops port

- Each port commit adds `core/plugins/<name>/` and deletes the Python folder in the same commit. That folder holds `awm-plugin.toml`, the crate `awm-plugin-<name>` with binary `<name>`, and any pages.
- Each port removes the plugin's rows from `scripts/run-tests.sh` and adds, in the same commit, a `run.sh` that is one line, `exec` of the binary, and the binary's `[artifacts]` pin line in `deploy.toml`, in the pin format of PLAN.md T2.
- Build and test only through `scripts/rust-build.sh` with `AWM_CARGO_TARGET_DIR=$HOME/.cache/awm-target/port-ops`.
- Submit with `scripts/land.sh submit --brief <file> --restart <plugin>`. Then run the live check on mizar and on one other node.
- SQLite uses `rusqlite` with `bundled`, through the knowledge group's store crate (`awm-store`) when it has landed. Otherwise the plugin opens the database directly with the same path rule and switches to the store crate later.
- The SDK needs listed per plugin go to plugin-redux/interview. No plugin builds a private copy.

## Plan

**Replaces.** `awm/service_components/grantlib/verify_grant.py` (96 src, plus `make_vectors.py` 85, test 24 over 13 vector cases) and the rest of `awm/service_components/trustpolicy/` (298 src, 204 test) that the crate lacks. The crate today holds `edges.rs` 310, `grant.rs` 200, `trust.rs` 286 and `port.rs` 99. Both Python libraries stay (rule 5): zotero and claude-science keep grantlib, and trilium keeps trustpolicy.

**API to add.**
- grant:
  - `b64u` and `unb64u`, which accept input with or without padding.
  - `verify_order(envelope, key, node, now) -> Order` (order_id, kind, node, payload, not_before default 0, not_after, raw body).
  - `sign_envelope(field, body, &SigningKey) -> {field: body, sig, key_id}`, byte-identical to the approver's `Keys.envelope`.
  - `grant_key_env`, which moves from the gateway's `install::grant_key`, and `grant_key_file(awm_dir)` for reflection's `<AWM_DIR>/grant-key.pub`.
- Fixes to `verify_grant`:
  - Split `NotYetValid` from `Expired`.
  - Default `not_before` to 0, and `subject` and `payload_sha256` to empty.
  - Check in Python's order: key id, then floats, then signature.
  - Return the raw body beside the typed grant.
  - Make each error's `Display` match the Python message.
- trust:
  - `Level::ALL`.
  - `FIRST_LOCAL_MESSAGE_HINT`, and the effort ceiling from reflection (harness group request).
  - `JournalBound::may_read` and `may_write`, keyed `"{project}/{scope}"`, where None means unbounded.
  - `level_for_start` takes an optional node.
- edges: `label(name)`.
- privacy: the gateway's `privacy.rs` moves into this crate as `awm_trust::privacy`, with the private-mark writer that scopes needs, so the gateway, dvc and scopes share one reader. The knowledge group makes that move in its own T4 and announces it before submitting. T1 keeps the crate's module layout open for it and does not move the file itself.
- requests: `file_request` and `request` over a caller-supplied async call closure, so the crate stays free of the SDK. Keep the 15 to 1440 minute bound and the non-empty reason rule.
- Not ported: `admin_cert`, `trust_raise`, `knowledge_invite` and `browser_signin`. Nothing outside the tests calls them.

**Callers.**

| Piece | Caller |
|---|---|
| portal grants | auth |
| plugin_commits grants | the gateway |
| `verify_order`, the signer | operations |
| `level_for_start`, `may_steer`, the hint, the ceiling | reflection |
| `journal_bound` | journal |
| `Level` | social, browser |
| `file_request` | browser |
| `privacy` reader | the gateway, dvc, scopes |
| `privacy` writer | scopes |

**Rust crates added.** None. ed25519-dalek, sha2, base64, hex, serde_json and thiserror are already its dependencies.

**Test plan.**
1. Extend `make_vectors.py` with new cases. The 13 existing cases stay byte-identical. The new cases cover: not-yet-valid next to expired, a missing `not_before`, a nested float, an integer above 2^53, non-ASCII keys, canonical bytes and digests, `b64u` round trips, signer vectors, and orders.
2. Add `trust_vectors.json`, generated from trustpolicy. It covers `level_for_start` over every level pair with and without a grant and a node, `may_steer` against every level, `journal_bound`, and `may_read`/`may_write`. The Python tests and the Rust tests both read it.
3. Port the remaining unit cases.
4. Run the gateway's grant-key tests against the moved function.

**Landing.**
1. One Python commit: the vector scripts and their JSON. The Python tests stay green.
2. One Rust commit: the crate additions and the move of `grant_key`. The gateway and CLI tests stay green.
3. Land T1 first in the batch. It restarts no plugin.

Gotchas: the 13 existing vectors must not change a byte. Ed25519 is deterministic, so the `git diff` of `test_vectors.json` must show only additions. Python's base64 discards characters outside the alphabet and Rust refuses them, so Rust is stricter by choice. Moving `grant_key` changes the gateway binary, so it ships with the next core pin.

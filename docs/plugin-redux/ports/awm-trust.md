# Port plan: awm-trust

## Purpose & Contents

This file holds the approved port plan for awm-trust, from task T1 of the ops group's plan (task CWPYAtyb), revised and approved on 2026-11-06. It names only what changes. The Python code and its manifests are the reference for everything the port keeps. The rules that hold for every ops port are in [README.md](README.md). Later changes go in the port commits and the journal.

## Extend awm-trust with the grant, order, signer and trust helpers

Add:
- `b64u` and `unb64u`. Decoding takes input with or without padding.
- `verify_order`.
- `sign_envelope`, byte-identical to the approver's `Keys.envelope`.
- `grant_key_env`, moved from the gateway's `install::grant_key`, and `grant_key_file`, which reads `<AWM_DIR>/grant-key.pub`.
- `requests::file_request` and `request`, over a caller-supplied async call, with the 15 to 1440 minute bound.
- `Level::ALL`, `FIRST_LOCAL_MESSAGE_HINT`, the effort ceiling (high is the cap), and `JournalBound::may_read` and `may_write`.
- An optional node for `level_for_start`.

Fix `verify_grant` to match Python:
- split `NotYetValid` from `Expired`;
- default `not_before` to 0;
- run the checks in Python's order;
- return the raw body;
- give each error Python's message text.

Do not port `admin_cert`, `trust_raise`, `knowledge_invite` or `browser_signin`. They have no callers. Node labels and privacy marks come from the SDK.

Tests:
- Extend `make_vectors.py`. Its 13 existing cases stay byte-identical.
- Add `core/crates/awm-trust/tests/trust_vectors.json`, generated from trustpolicy.
- The Python and Rust suites both read both files. The Rust suite also checks the error text.

Landing: one Python commit for the vectors, then one Rust commit. Neither restarts anything.

Gotchas:
- The redux branch also edits awm-trust's door.rs and port.rs. T1 touches only grant.rs, trust.rs, lib.rs and requests.rs, so merge first.
- Rust base64 refuses characters that Python drops. That strictness is deliberate.


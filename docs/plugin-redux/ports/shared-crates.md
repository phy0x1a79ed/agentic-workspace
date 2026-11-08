# Port plan: comms shared crates

## Purpose & Contents

This file holds the approved port plan for awm-cdp, the SDK push module, the lockfile change and the `state.toml` files, from the comms group's plan (task CWPYAtyb), approved by Tony on 2026-11-06. It names only what the Python does not make obvious. The Python at release 49232bf9 is the reference for everything kept. The rules every comms port follows are in `comms.md`.

## Plan

1. One lockfile commit adds the new dependencies: `spake2`, `hkdf`, `chacha20poly1305`, `sha2`, `sha1`, `rand`, the twilight crates, `async-imap`, `tokio-rustls`, `mail-parser` and `lettre`. All are pure Rust with rustls.
2. `awm_plugin_sdk::push` is the twin of `push.py`, in its own additive commit,. The SDK already has holders, sessions, supervise and proc, so there is no awm-holders crate. It keeps one ordered queue per `(node, job)`, and its result is `Delivered`, `Retry` or `Gone` (from reflection's `code: "gone"`). It never drops a line: past its retry window it hands that line and every line behind it back, in order.
3. `core/crates/awm-cdp` serves browser and rlm-browser. It holds the CDP connection (flat sessions and per-target sockets, unlimited frames, reconnect), `/json/version` and `/json/protocol`, `wait_ready`, `free_port`, the ws-path rewrite, a `docker` CLI runner, and `find_chrome()`. `find_chrome()` tries `RLM_CHROME_BIN`, then `google-chrome`, `google-chrome-stable`, `chromium` and `chromium-browser` on `PATH`.
4. tether and kept are modules, not crates, since each has one consumer. kept's `transcript_tail` goes to awm-harness.
5. Each plugin's `state.toml` copies everything except data another program owns, such as Chrome profiles and Docker's data, which is `[[size_only]]`:
   - social: `social.db` (712 KB on alcor) with its v9 tables `[[additive]]`, plus `social.toml`;
   - channel: `services/channel`, `services/relay` (`messages` is `[[transient]]`) and `services/tether`, with the tether units as `[[extra]]`;
   - browser: `browser.json` and `seats/` under `/var/lib/awm/browser` only, with `seats.json` `[[additive]]` and leases `[[transient]]`. The Docker data root, `.docker` and `profile/` (Tony's Chrome logins) are `[[size_only]]`.
   - Each copy lands at `<dir>/.pre-rust/`, and SQLite goes through the backup API, so a live WAL DB does not tear.
6. awm-store must adopt Python's one-row `schema_version` table and an unversioned DB, as both `channels.db` files are. It never takes its fresh-DB path on a DB that has tables. The exact rule is settled with knowledge, which owns awm-store.

Gotchas: a lockfile change rebuilds every plugin but restarts only plugins whose binary bytes changed, so check that on the first refresh.

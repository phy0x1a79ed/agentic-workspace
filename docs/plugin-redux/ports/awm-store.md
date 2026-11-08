# Port plan: awm-store

The SQLite half of `persistence`, depending on no other awm crate:
- `Store::open`: a small pool with `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000` and `foreign_keys=ON`. An inode change (a restored file) drops the pool. `read` and `write` run in `spawn_blocking`, and `write` uses `BEGIN IMMEDIATE`. Also `blocking_*`, a dedicated `connection()`, and `open_ro`.
- `Store::init(schema, version, migrations)`:
  - A fresh DB runs the schema.
  - An existing one first writes a `VACUUM INTO` backup `<db>.v<from>.bak`, never overwriting an existing one (it uses `<db>.v<from>.<unix-ms>.bak` instead). A failed backup refuses the start.
  - It refuses a version above its own.
- **CAUTION** The migration loop runs each step with `execute_batch` in autocommit, and treats `duplicate column name` and `no such table` as done. Python's crash-resume depends on this, so wrapping a step in a transaction changes behaviour.
- Also `rows_json` (a blob is an error), `settings` (the `awm_settings` table), `config::Contract` for ops, `new_uuid` (32 hex), and the `time` helpers from awm-pycompat.
- rusqlite 0.41 `bundled` gives SQLite 3.54.1 with FTS5, the same version as the awm env's Python.

Placement: `config_service.py` dies with scopes, because only dead vagrant code reads it. The `ConfigContract` storage goes into `settings`, and its protocol into the SDK. The Python library stays for trilium, vpn and rlm-factorio.

Tests: port `test_databases.py`. Cover the migration edge cases. Run every consumer's real schema and migrations through `init` against a copy of its mizar DB, expecting no change.

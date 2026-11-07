# Port plan: awm-dvcdata

## Purpose & Contents

This file holds the approved port plan for awm-dvcdata, from task T2 of the ops group's plan (task CWPYAtyb), revised and approved on 2026-11-06. It names only what changes. The Python code and its manifests are the reference for everything the port keeps. The rules that hold for every ops port are in [README.md](README.md). Later changes go in the port commits and the journal.

## Build awm-dvcdata

Port `awm/service_components/dvcdata` and dvc's `cache.py`. Do natively everything that Python asks the `dvc` CLI for: checkout, status, data status, the merge driver and the three hooks. After the port, no plugin runs DVC. It stays a hand tool for `dvc add`.

Format facts:
- The cache is DVC 3 `files/md5`, with objects hardlinked at mode 0444.
- A file id is the plain md5 of its bytes.
- A `.dir` id is the md5 of the manifest bytes. Those bytes are exactly Python's `json.dumps(sorted entries, sort_keys=True)`.
- A legacy pin without `hash: md5` is refused and never hashed.

Checkout follows DVC 3:
1. An inode match counts as unchanged. Otherwise compare size, then md5.
2. Remove the files that the pin dropped.
3. Refuse to replace content that is not in the cache.
4. Link with a hardlink, else a symlink.

gc is a verbatim port of every safety rule, and dry-run stays the verb's default. `cachetag` is re-exported from awm-statecheck, which owns the signature.

For scopes (knowledge):
- Every function scopes uses maps one to one.
- `status::changed_outs` replaces `dvc status --json`, and `dvc_available` goes.
- scopes and dvc both pass `<AWM_DIR>/bin/dvc` as the hook binary.
- The first wiring after the port copies each awm hook to `<hook>.awm-py.bak`. Foreign hooks stay untouched.

Tests:
- Port the 55 Python tests.
- Round-trip every real `.dir` object on mizar and on one backup node.
- Hash a CRLF file and a non-ASCII path.
- Run DVC interop tests behind `AWM_DVC_BIN`: native checkout against `dvc status`, both merge drivers, and `changed_outs`.

One commit adds the crate and restarts nothing.

Gotchas:
- A checkout that diverges from DVC can delete data. The interop suite and the unsaved-content refusal guard against it.
- Survey every node for legacy pins first.
- Land dvc and scopes close together, or each one keeps rewriting the other's hooks.


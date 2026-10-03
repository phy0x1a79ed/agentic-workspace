"""Seeding driver for the precedence archive (T2/T3).

Two subcommands:

- ``scrape`` — invoke the pure :mod:`scrape` functions over the workspace's
  existing history (feedback memories, operator posts, journal decisions) and
  emit ONE **candidate** staging-manifest JSON. It never touches the DB; the
  output is meant for hand curation before import.
- ``import`` (default) — load a curated staging-manifest JSON into the
  service's own DB directly through :mod:`store` (no gateway round-trip), so a
  bulk seed runs offline. Local counterpart of the ``precedence_import`` verb;
  idempotent on the same stable ids.

Usage:
    python -m awm.precedence.seed scrape --out /path/to/candidates.json \\
        [--memory-dir DIR] [--scopes-db PATH] [--all-journal] [--no-operator]
    python -m awm.precedence.seed /path/to/staging.json [--no-embed]
    python -m awm.precedence.seed import /path/to/staging.json [--no-embed]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import dao, scrape, store

# Default source locations (workspace-level; overridable on the CLI).
DEFAULT_MEMORY_DIR = (
    "/home/tony/.claude/projects/"
    "-home-tony-agentic-workspace-projects-awm--bare/memory"
)
DEFAULT_SCOPES_DB = "/home/tony/agentic_workspace/.awm/services/scopes/scopes.db"


def run_import(manifest_path: str, *, embed_after: bool = True) -> dict:
    """Load a curated staging manifest into the precedence DB. Idempotent."""
    dao.init()
    conn = dao.connect()
    try:
        return store.import_manifest(conn, manifest_path=manifest_path, embed_after=embed_after)
    finally:
        conn.close()


def _flag(argv: list[str], name: str) -> bool:
    return name in argv


def _opt(argv: list[str], name: str, default: str) -> str:
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def _cmd_scrape(argv: list[str]) -> int:
    memory_dir = _opt(argv, "--memory-dir", DEFAULT_MEMORY_DIR)
    scopes_db = _opt(argv, "--scopes-db", DEFAULT_SCOPES_DB)
    out = _opt(argv, "--out", "")
    manifest = scrape.build_candidates(
        memory_dir=memory_dir,
        scopes_db=scopes_db,
        include_operator=not _flag(argv, "--no-operator"),
        include_journal=not _flag(argv, "--no-journal"),
        journal_user_marked_only=not _flag(argv, "--all-journal"),
    )
    payload = json.dumps(manifest, indent=2, ensure_ascii=False)
    if out:
        Path(out).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    by_source: dict[str, int] = {}
    for d in manifest["decisions"]:
        by_source[d["source"]] = by_source.get(d["source"], 0) + 1
    dest = out or "(stdout)"
    print(
        f"precedence.seed scrape: {len(manifest['decisions'])} candidates "
        f"{by_source} -> {dest}",
        file=sys.stderr,
    )
    return 0


def _cmd_import(manifest_path: str, argv: list[str]) -> int:
    embed_after = not _flag(argv, "--no-embed")
    counts = run_import(manifest_path, embed_after=embed_after)
    print(
        f"precedence.seed: imported {counts['imported']} decisions "
        f"({counts['changed']} new/changed), embedded {counts['embedded']}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    if argv[0] == "scrape":
        return _cmd_scrape(argv[1:])
    if argv[0] == "import":
        if len(argv) < 2:
            print("usage: python -m awm.precedence.seed import <manifest.json>", file=sys.stderr)
            return 2
        return _cmd_import(argv[1], argv[2:])
    # Back-compat: bare `seed <manifest.json>` still imports.
    return _cmd_import(argv[0], argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())

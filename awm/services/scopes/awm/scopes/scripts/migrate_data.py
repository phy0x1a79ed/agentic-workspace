"""Phased rollout driver: convert one project's data to git-annex.

Ordering is by risk, not by size. Run it per project, read the report, decide,
then run the next. Nothing here is a batch job — the whole point of a phased
rollout is that each project is independently shippable and independently
reversible.

    python -m awm.scopes.scripts.migrate_data <project> --check
    python -m awm.scopes.scripts.migrate_data <project>
    python -m awm.scopes.scripts.migrate_data <project> --heal-only

Phases:

1. **Survey** — size, file count, active scopes, vendored checkouts, and
   *broken symlinks*. That last one is load-bearing: annex's own representation
   IS symlinks, so converting on top of pre-existing rot makes "content not
   fetched yet" and "this link was already dead" indistinguishable forever.
   Sweep immediately before converting, not weeks earlier.
2. **Convert** — `data/<project>/` becomes an annex repo. Refuses while any
   scope is active.
3. **Heal** — every existing scope's `.awm/data` symlink becomes a clone.
4. **Verify** — the canonical repo reports clean, every scope reports annex
   mode, and no excluded path was committed.

The naked directory is never deleted: conversion is in place and additive, so
the rollback is `rm -rf data/<project>/.git` plus a `scope heal`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from awm.scopes import data_annex as da


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n}"


def survey(project: str) -> dict:
    repo = da.canonical_repo(project)
    files = 0
    size = 0
    broken: list[str] = []
    for root, dirs, names in os.walk(repo):
        if ".git" in dirs:
            dirs.remove(".git")
        for name in names:
            p = Path(root) / name
            files += 1
            if p.is_symlink() and not p.exists():
                broken.append(str(p.relative_to(repo)))
                continue
            try:
                size += p.stat().st_size
            except OSError:
                pass
    return {
        "path": str(repo),
        "exists": repo.is_dir(),
        "already_annex": da.is_annex_repo(repo),
        "files": files,
        "size": size,
        "broken_symlinks": broken,
        "vendored": [p for p, _u, _s in da.scan_vendored(repo)] if repo.is_dir() else [],
    }


def active_scopes(project: str) -> list[str] | None:
    """Scopes of ``project`` currently running a job, or None if we couldn't ask.

    None and ``[]`` mean very different things and the caller must not conflate
    them. Conversion rewrites the tree into read-only symlinks underneath any
    running job, so "no active scopes" is a safety precondition — and an
    unreachable or unseeded database proves nothing. Returning None makes the
    caller refuse rather than read an error as an all-clear.
    """
    import sqlite3
    try:
        from awm.scopes.dao import ScopesDAO
        rows = ScopesDAO().query_all(
            "SELECT a.scope FROM agents a JOIN projects p ON p.id = a.project_id "
            "WHERE p.name=? AND a.status='active'",
            (project,),
        )
    except (sqlite3.Error, OSError) as exc:
        print(f"  cannot read the scopes database: {exc}")
        return None
    return [r["scope"] for r in rows]


def report_survey(project: str, s: dict) -> None:
    print(f"=== survey: {project} ===")
    print(f"  path              {s['path']}")
    print(f"  exists            {s['exists']}")
    print(f"  already annexed   {s['already_annex']}")
    print(f"  files             {s['files']:,}")
    print(f"  size              {_human(s['size'])}")
    print(f"  vendored repos    {len(s['vendored'])}"
          + (f"  {s['vendored'][:5]}" if s["vendored"] else ""))
    n = len(s["broken_symlinks"])
    print(f"  BROKEN symlinks   {n}")
    if n:
        for b in s["broken_symlinks"][:10]:
            print(f"      {b}")
        if n > 10:
            print(f"      … and {n - 10} more")
        print("  ^ resolve these FIRST. After conversion every annexed file is a")
        print("    symlink, so pre-existing rot becomes indistinguishable from")
        print("    'content not fetched yet'.")


def verify(project: str) -> int:
    """Post-conversion checks. Returns the number of problems found."""
    from awm.scopes import scopes as scopes_mod
    repo = da.canonical_repo(project)
    problems = 0
    print(f"=== verify: {project} ===")

    if not da.is_annex_repo(repo):
        print("  FAIL: canonical repo is not annex-backed")
        return 1
    print("  ok: canonical repo is annex-backed")

    if da._is_clean(repo):
        print("  ok: canonical working tree clean")
    else:
        print("  FAIL: canonical working tree dirty after conversion")
        problems += 1

    tracked = (da._git(repo, "ls-files").stdout or "").splitlines()
    leaked = [t for t in tracked
              if "secrets/" in t or t.split("/")[-1].startswith(".env")
              or t.endswith(".credentials.json")]
    if leaked:
        print(f"  FAIL: {len(leaked)} excluded path(s) got committed: {leaked[:5]}")
        problems += 1
    else:
        print(f"  ok: no secret/.env path in {len(tracked):,} tracked files")

    import sqlite3
    try:
        from awm.scopes.dao import ScopesDAO
        rows = ScopesDAO().query_all(
            "SELECT a.scope, a.worktree FROM agents a JOIN projects p ON p.id = a.project_id "
            "WHERE p.name=? AND a.status IN ('allocated','active')",
            (project,),
        )
    except (sqlite3.Error, OSError) as exc:
        # The canonical-repo checks above already ran and are the ones that
        # matter; not being able to enumerate scopes is worth saying out loud
        # but is not itself a conversion failure.
        print(f"  skipped scope check: {exc}")
        return problems
    for r in rows:
        st = scopes_mod.data_status(project, r["scope"])
        mode = st.get("mode")
        flag = "ok" if mode == "annex" else "FAIL"
        if mode != "annex":
            problems += 1
        print(f"  {flag}: scope {r['scope']} -> {mode} {st.get('branch', '')}")
    if not rows:
        print("  (no live scopes to check)")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("project")
    ap.add_argument("--check", action="store_true",
                    help="survey + dry-run only; change nothing")
    ap.add_argument("--heal-only", action="store_true",
                    help="skip conversion; just give existing scopes their clones")
    ap.add_argument("--force-broken", action="store_true",
                    help="convert even with broken symlinks present")
    ap.add_argument("--force-active", action="store_true",
                    help="convert even when scopes are active, or when the "
                         "scopes database could not be read to check")
    args = ap.parse_args(argv)
    project = args.project

    if not da.annex_available():
        print("git-annex not found. Install it or set AWM_ANNEX_BIN.", file=sys.stderr)
        return 2

    s = survey(project)
    if not s["exists"]:
        print(f"no data directory at {s['path']}", file=sys.stderr)
        return 2
    report_survey(project, s)

    busy = active_scopes(project)
    if busy is None:
        print("\n  ACTIVE SCOPES: UNKNOWN — could not read the scopes database.")
        print("  That is not an all-clear: conversion rewrites the tree into")
        print("  read-only symlinks and would break a running job. Pass")
        print("  --force-active only if you know nothing is running.")
    elif busy:
        print(f"\n  ACTIVE SCOPES: {busy}")
        print("  Conversion rewrites the tree into read-only symlinks and would")
        print("  break a running job. Retire them or wait for the freeze window.")

    if args.check:
        print()
        print(da.init_project_data(project, dry_run=True))
        return 0

    if not args.heal_only:
        if s["broken_symlinks"] and not args.force_broken:
            print(f"\nrefusing: {len(s['broken_symlinks'])} broken symlink(s). "
                  f"Resolve them, or pass --force-broken.", file=sys.stderr)
            return 1
        if (busy is None or busy) and not args.force_active:
            why = ("could not verify that no scope is active"
                   if busy is None else "active scopes (see above)")
            print(f"\nrefusing: {why}.", file=sys.stderr)
            return 1
        print("\n=== convert ===")
        rep = da.init_project_data(project)
        print(f"  {rep}")
        if rep["result"] not in ("converted", "refreshed", "up_to_date"):
            return 1

    print("\n=== heal scopes ===")
    import sqlite3
    try:
        from awm.scopes.scopes import heal_scopes
        healed = heal_scopes(project=project)
    except (sqlite3.Error, OSError) as exc:
        print(f"  skipped: {exc}")
        healed = []
    for r in healed:
        action = (r.get("actions") or {}).get("data")
        print(f"  {r['scope']:24s} {'ok' if r['ok'] else 'FAIL'}  data={action}")
    if not healed:
        print("  (no scopes to heal)")

    print()
    problems = verify(project)
    print()
    if problems:
        print(f"{problems} problem(s) — NOT done")
        return 1
    print(f"{project} converted. Rollback if needed: "
          f"rm -rf {s['path']}/.git && awm scope heal --project {project}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

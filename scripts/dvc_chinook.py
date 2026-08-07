#!/usr/bin/env python3
"""dvc_chinook.py — hash-selective sync of DVC cache objects against chinook.

WHY THIS EXISTS
backup_to_chinook.sh mirrors the whole workspace up to chinook nightly, which
covers the *push* direction. It has no inverse: there is no way to bring back
just the objects one scope needs. Restoring by re-mirroring is all-or-nothing
against an 86 GB / 53k-object cache, which is the wrong tool for "this scope is
missing three pins".

This script closes that gap. It reads a scope's *.dvc pins, resolves them to the
exact set of cache objects they depend on, diffs that against the local cache,
and moves only the difference.

THE TWO-PHASE PROBLEM
A directory pin names a *.dir manifest, not the files under it:

    outs:
    - md5: 4c9eb65ac2ca0405305a684016920762.dir

That manifest is itself a cache object whose content is a JSON array of
{"md5", "relpath"} leaves. So the leaf hashes are unknowable until the manifest
is in hand — a cold restore needs manifests fetched and parsed *before* the
real payload can even be named. `pull` therefore may submit two Globus tasks.

WHY NOT A DVC REMOTE
DVC's remote contract is per-file exists/get_file/put_file. The Globus Transfer
API is asynchronous and batch: a task takes seconds to start and minutes to
finish. Per-object that is unusable, so chinook cannot be a DVC remote. With a
shared cache, scopes don't need one anyway — `dvc checkout` materializes from
cache with no remote involved. This script is the chinook interface; `dvc
checkout` is the materialization step.

    dvc_chinook.py status --scope projects/fabfos/dev
    dvc_chinook.py pull   --scope projects/fabfos/dev [--dry-run]
    dvc_chinook.py push   --scope projects/fabfos/dev [--dry-run]
    dvc_chinook.py resolve --scope projects/fabfos/dev   # print the object set
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

WS_ROOT = Path(os.environ.get("AWM_WORKSPACE_ROOT", "/home/tony/agentic_workspace"))
GLOBUS = Path(os.environ.get("GLOBUS_BIN", "/home/tony/lib/miniforge3/envs/globus/bin/globus"))

# Same endpoints backup_to_chinook.sh uses. SRC is this machine's GCP endpoint;
# DST is the chinook collection. Kept in sync with that script by hand — if a
# transfer starts failing with an endpoint error, check there first.
LOCAL_EP = os.environ.get("DVC_CHINOOK_LOCAL_EP", "57b23332-9048-11f1-ad24-02ce27bde401")
CHINOOK_EP = os.environ.get("DVC_CHINOOK_EP", "2602486c-1e0f-47a0-be15-eec1b0ff0f96")
# Must match backup_to_chinook.sh's DST_PATH: that job is what puts the cache
# there in the first place, and a pull reads back exactly what it wrote.
# Overridable only so the round trip can be exercised against a scratch path
# without racing the nightly mirror, which runs delete_destination_extra here.
CHINOOK_PREFIX = os.environ.get("DVC_CHINOOK_PREFIX", "/Workspace_backups/Tony_Liu/altair")


def note(msg: str) -> None:
    print(f"[dvc-chinook] {msg}", file=sys.stderr)


# --- cache location ---------------------------------------------------------

def cache_dir_for(scope: Path) -> Path:
    """Resolve a scope's DVC cache dir. config.local wins over config, matching
    DVC's own precedence; the AWM scopes all point at one shared cache."""
    found = None
    for name in ("config", "config.local"):
        cfg = scope / ".dvc" / name
        if not cfg.exists():
            continue
        section = None
        for raw in cfg.read_text().splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip().strip('"')
            elif section == "cache" and line.startswith("dir"):
                _, _, val = line.partition("=")
                found = val.strip().strip('"')
    if found:
        return Path(found) if os.path.isabs(found) else (scope / ".dvc" / found).resolve()
    return scope / ".dvc" / "cache"


def object_relpath(h: str) -> str:
    """Cache-relative path for a hash. DVC 3.x shards on the first two chars and
    keeps the remainder as the filename. A .dir hash carries its suffix in the
    *filename*, so the same split works for manifests and leaves alike."""
    return f"files/md5/{h[:2]}/{h[2:]}"


# --- pin resolution ---------------------------------------------------------

def find_pins(scope: Path) -> list[Path]:
    """Every *.dvc file under the scope. Excludes anything inside the .dvc/
    config directory, which shares the extension but holds no pins. The match
    must be on a path *component*: a substring test also excludes every real
    pin, since "swissprot.dvc" ends in ".dvc" too."""
    return sorted(
        p for p in scope.rglob("*.dvc")
        if p.is_file() and ".dvc" not in p.relative_to(scope).parts[:-1]
    )


def pin_hashes(pin: Path) -> list[str]:
    """Top-level object hashes a pin declares. Only `outs` — `deps` point at
    other stages' outputs, which their own pins already cover."""
    try:
        doc = yaml.safe_load(pin.read_text()) or {}
    except yaml.YAMLError as e:
        note(f"skipping unparseable pin {pin}: {e}")
        return []
    out = []
    for o in doc.get("outs") or []:
        if isinstance(o, dict) and o.get("md5"):
            out.append(str(o["md5"]))
    return out


def expand_manifest(h: str, cache: Path) -> list[str]:
    """Leaf hashes inside a .dir manifest that is present locally."""
    f = cache / object_relpath(h)
    try:
        entries = json.loads(f.read_text())
    except (OSError, json.JSONDecodeError) as e:
        note(f"cannot read manifest {h}: {e}")
        return []
    return [str(e["md5"]) for e in entries if isinstance(e, dict) and e.get("md5")]


def resolve(scope: Path, cache: Path) -> tuple[set[str], set[str], set[str]]:
    """Resolve a scope to its full object set.

    Returns (all_objects, manifests, unresolved_manifests) where
    unresolved_manifests are .dir hashes absent locally — their leaves are
    unknown until those manifests are fetched, which is what makes pull
    two-phase.
    """
    tops: set[str] = set()
    for pin in find_pins(scope):
        tops.update(pin_hashes(pin))

    manifests = {h for h in tops if h.endswith(".dir")}
    objects = set(tops)
    unresolved = set()

    for h in manifests:
        if (cache / object_relpath(h)).exists():
            objects.update(expand_manifest(h, cache))
        else:
            unresolved.add(h)

    return objects, manifests, unresolved


def split_present(objects: set[str], cache: Path) -> tuple[set[str], set[str]]:
    present, missing = set(), set()
    for h in objects:
        (present if (cache / object_relpath(h)).exists() else missing).add(h)
    return present, missing


# --- globus ----------------------------------------------------------------

def globus_json(args: list[str], body: dict | None = None) -> str:
    cmd = [str(GLOBUS)] + args
    kwargs: dict = {"capture_output": True, "text": True}
    if body is not None:
        kwargs["input"] = json.dumps(body)
        cmd += ["--body-file", "-"]
    r = subprocess.run(cmd, **kwargs)
    if r.returncode != 0:
        raise RuntimeError(f"globus {' '.join(args)} failed:\n{r.stderr.strip()}")
    return r.stdout.strip()


def transfer_items(hashes: set[str], cache: Path, direction: str) -> list[dict]:
    """One transfer_item per object. Explicit paths, never filter_rules: Globus
    filters match on item *name* at any depth, which cannot express "this exact
    object" — the same reason backup_to_chinook.sh partitions by path."""
    rel_cache = cache.relative_to(WS_ROOT)
    items = []
    for h in sorted(hashes):
        rel = f"{rel_cache}/{object_relpath(h)}"
        local = str(WS_ROOT / rel)
        remote = f"{CHINOOK_PREFIX}/{rel}"
        src, dst = (remote, local) if direction == "pull" else (local, remote)
        items.append({
            "DATA_TYPE": "transfer_item",
            "source_path": src,
            "destination_path": dst,
            "recursive": False,
        })
    return items


def submit(items: list[dict], direction: str, label: str) -> str:
    src, dst = (CHINOOK_EP, LOCAL_EP) if direction == "pull" else (LOCAL_EP, CHINOOK_EP)
    sub_id = globus_json(["api", "transfer", "get", "/submission_id",
                          "--jmespath", "value", "--format", "unix"])
    body = {
        "DATA_TYPE": "transfer",
        "submission_id": sub_id,
        "source_endpoint": src,
        "destination_endpoint": dst,
        "label": label[:128],
        # 1 = copy when size differs. Objects are immutable and content-addressed,
        # so a same-size file is the right file; this still re-copies anything a
        # previous interrupted task left truncated. Checksum-level would mean
        # hashing both sides of an 86 GB store to learn nothing.
        "sync_level": 1,
        "verify_checksum": True,
        # Never. A restore must not be able to prune either side, and chinook is
        # already mirrored destructively by the nightly job — see that script.
        "delete_destination_extra": False,
        "DATA": items,
    }
    return globus_json(["api", "transfer", "post", "/transfer",
                        "--jmespath", "task_id", "--format", "unix"], body=body)


def wait(task_id: str, timeout: int = 3600) -> bool:
    note(f"waiting on task {task_id}")
    r = subprocess.run([str(GLOBUS), "task", "wait", task_id,
                        "--polling-interval", "15", "--timeout", str(timeout)])
    return r.returncode == 0


# --- commands ---------------------------------------------------------------

def cmd_status(scope: Path, cache: Path) -> int:
    objects, manifests, unresolved = resolve(scope, cache)
    present, missing = split_present(objects, cache)
    pins = find_pins(scope)
    print(f"scope        {scope}")
    print(f"cache        {cache}")
    print(f"pins         {len(pins)}")
    print(f"manifests    {len(manifests)} (.dir)  unresolved: {len(unresolved)}")
    print(f"objects      {len(objects)} known")
    print(f"  present    {len(present)}")
    print(f"  missing    {len(missing)}")
    if unresolved:
        print(f"\n{len(unresolved)} manifest(s) absent locally — their leaves are not yet")
        print("counted above. `pull` fetches manifests first, then the leaves.")
    return 0


def cmd_resolve(scope: Path, cache: Path) -> int:
    objects, _, unresolved = resolve(scope, cache)
    for h in sorted(objects):
        print(h)
    if unresolved:
        note(f"{len(unresolved)} unresolved manifest(s) — object set is incomplete")
    return 0


def cmd_pull(scope: Path, cache: Path, dry_run: bool) -> int:
    objects, _, unresolved = resolve(scope, cache)

    # Phase 1 — manifests must land before their leaves can be named.
    if unresolved:
        note(f"phase 1: {len(unresolved)} manifest(s) to fetch")
        items = transfer_items(unresolved, cache, "pull")
        if dry_run:
            print(json.dumps(items, indent=2))
            note("dry run — stopping before phase 2 (leaves are unknowable until "
                 "manifests are local)")
            return 0
        tid = submit(items, "pull", f"dvc-chinook manifests {scope.name}")
        note(f"submitted {tid}")
        if not wait(tid):
            note("manifest fetch failed")
            return 1
        objects, _, still = resolve(scope, cache)
        if still:
            note(f"{len(still)} manifest(s) still missing after fetch — aborting")
            return 1

    _, missing = split_present(objects, cache)
    if not missing:
        note("nothing to pull — all objects present")
        return 0

    note(f"phase 2: {len(missing)} object(s) to fetch")
    items = transfer_items(missing, cache, "pull")
    if dry_run:
        print(json.dumps(items, indent=2))
        return 0
    tid = submit(items, "pull", f"dvc-chinook pull {scope.name}")
    note(f"submitted {tid}")
    if not wait(tid):
        return 1
    # A pulled object is a fresh inode at mode 0644 — not the read-only,
    # many-times-hardlinked file DVC leaves behind. Nothing reconnects it to the
    # worktrees on its own, so checkout is required, not merely suggested.
    note("pull complete — run `dvc checkout` in the scope to materialize")
    return 0


def cmd_push(scope: Path, cache: Path, dry_run: bool) -> int:
    objects, _, unresolved = resolve(scope, cache)
    if unresolved:
        note(f"{len(unresolved)} manifest(s) missing locally — cannot push what "
             "isn't here; pull first")
        return 1
    present, missing = split_present(objects, cache)
    if missing:
        note(f"{len(missing)} object(s) absent locally and will be skipped")
    if not present:
        note("nothing to push")
        return 0
    note(f"{len(present)} object(s) to push")
    items = transfer_items(present, cache, "push")
    if dry_run:
        print(json.dumps(items, indent=2))
        return 0
    tid = submit(items, "push", f"dvc-chinook push {scope.name}")
    note(f"submitted {tid}")
    return 0 if wait(tid) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["status", "resolve", "pull", "push"])
    ap.add_argument("--scope", required=True,
                    help="scope worktree, absolute or relative to the workspace root")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the transfer items instead of submitting")
    a = ap.parse_args()

    scope = Path(a.scope)
    if not scope.is_absolute():
        scope = WS_ROOT / scope
    scope = scope.resolve()
    if not (scope / ".dvc").is_dir():
        note(f"not a DVC scope (no .dvc/): {scope}")
        return 2

    cache = cache_dir_for(scope)
    if not cache.is_dir():
        note(f"cache dir does not exist: {cache}")
        return 2
    try:
        cache.relative_to(WS_ROOT)
    except ValueError:
        note(f"cache {cache} is outside {WS_ROOT}; chinook paths are workspace-relative")
        return 2

    return {
        "status": lambda: cmd_status(scope, cache),
        "resolve": lambda: cmd_resolve(scope, cache),
        "pull": lambda: cmd_pull(scope, cache, a.dry_run),
        "push": lambda: cmd_push(scope, cache, a.dry_run),
    }[a.command]()


if __name__ == "__main__":
    sys.exit(main())

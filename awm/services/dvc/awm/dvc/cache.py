"""Resolving a DVC scope to the exact set of cache objects it depends on.

Everything here is pure local filesystem reasoning — no Globus, no network. It
answers one question: *given a worktree, which content-addressed objects must be
in the cache for `dvc checkout` to succeed?* :mod:`awm.dvc.transfer` turns the
answer into transfer items.

THE TWO-PHASE PROBLEM
A directory pin names a ``.dir`` manifest, not the files under it::

    outs:
    - md5: 4c9eb65ac2ca0405305a684016920762.dir

That manifest is itself a cache object whose content is a JSON array of
``{"md5", "relpath"}`` leaves — so on a cold cache the leaf hashes are
*unknowable* until the manifest is in hand. :func:`resolve` reports those
manifests separately as ``unresolved``, which is what forces ``pull`` to run in
two Globus tasks rather than one.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("awm.dvc.cache")


class DvcScopeError(Exception):
    """Raised when a path is not a usable DVC scope, or its cache is unusable."""


@dataclass
class Resolution:
    """The object set a scope depends on, split by what we can currently know."""

    objects: set[str] = field(default_factory=set)
    """Every object hash the scope needs, as far as is currently knowable."""

    manifests: set[str] = field(default_factory=set)
    """The ``.dir`` hashes among the top-level pins."""

    unresolved: set[str] = field(default_factory=set)
    """Manifests absent locally — their leaves are NOT counted in ``objects``."""

    present: set[str] = field(default_factory=set)
    missing: set[str] = field(default_factory=set)
    pins: int = 0


def cache_dir_for(scope: Path) -> Path:
    """Resolve a scope's DVC cache dir.

    ``config.local`` wins over ``config``, matching DVC's own precedence. The AWM
    scopes all point at one shared cache, which is why a per-scope pull is
    worthwhile at all: objects fetched for one scope are immediately available to
    every sibling that pins them.
    """
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
    """Cache-relative path for a hash.

    DVC 3.x shards on the first two characters and keeps the remainder as the
    filename. A ``.dir`` hash carries its suffix in the *filename*, so the same
    split serves manifests and leaves alike.
    """
    return f"files/md5/{h[:2]}/{h[2:]}"


def find_pins(scope: Path) -> list[Path]:
    """Every ``*.dvc`` pin file under the scope.

    Excludes anything inside the ``.dvc/`` config directory, which shares the
    extension but holds no pins. The match must be on a path *component*: a
    substring test would also exclude every real pin, since ``swissprot.dvc``
    ends in ``.dvc`` too.
    """
    return sorted(
        p
        for p in scope.rglob("*.dvc")
        if p.is_file() and ".dvc" not in p.relative_to(scope).parts[:-1]
    )


def pin_hashes(pin: Path) -> list[str]:
    """Top-level object hashes a pin declares.

    Only ``outs`` — ``deps`` point at other stages' outputs, which those stages'
    own pins already cover.
    """
    try:
        doc = yaml.safe_load(pin.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("skipping unparseable pin %s: %s", pin, exc)
        return []
    return [
        str(o["md5"])
        for o in (doc.get("outs") or [])
        if isinstance(o, dict) and o.get("md5")
    ]


def expand_manifest(h: str, cache: Path) -> list[str]:
    """Leaf hashes inside a ``.dir`` manifest that is present locally."""
    f = cache / object_relpath(h)
    try:
        entries = json.loads(f.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("cannot read manifest %s: %s", h, exc)
        return []
    return [str(e["md5"]) for e in entries if isinstance(e, dict) and e.get("md5")]


def resolve(scope: Path, cache: Path) -> Resolution:
    """Resolve a scope to its object set, split into present / missing / unresolved."""
    tops: set[str] = set()
    pins = find_pins(scope)
    for pin in pins:
        tops.update(pin_hashes(pin))

    res = Resolution(objects=set(tops), pins=len(pins))
    res.manifests = {h for h in tops if h.endswith(".dir")}

    for h in res.manifests:
        if (cache / object_relpath(h)).exists():
            res.objects.update(expand_manifest(h, cache))
        else:
            res.unresolved.add(h)

    for h in res.objects:
        target = res.present if (cache / object_relpath(h)).exists() else res.missing
        target.add(h)

    return res


def open_scope(scope_path: str) -> tuple[Path, Path]:
    """Validate a scope path and return ``(scope, cache)``.

    Rejects a cache outside the workspace root, because chinook paths are
    addressed workspace-relative: an outside cache has no representable location
    on the collection, and would otherwise be silently pushed to the wrong place.
    """
    from awm.dvc.config import WORKSPACE_ROOT

    scope = Path(scope_path)
    if not scope.is_absolute():
        scope = WORKSPACE_ROOT / scope
    scope = scope.resolve()

    if not (scope / ".dvc").is_dir():
        raise DvcScopeError(f"not a DVC scope (no .dvc/): {scope}")

    cache = cache_dir_for(scope)
    if not cache.is_dir():
        raise DvcScopeError(f"cache dir does not exist: {cache}")
    try:
        cache.relative_to(WORKSPACE_ROOT)
    except ValueError:
        raise DvcScopeError(
            f"cache {cache} is outside {WORKSPACE_ROOT}; chinook paths are "
            "addressed workspace-relative and cannot name it"
        ) from None

    return scope, cache

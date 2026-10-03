"""``awm deploy`` — one safe command to ship the release worktree to prod.

Everything the old bounce runbook did by hand, in the right order and
idempotently: conditionally (re)install the dist set, conditionally rebuild
changed pages, restart the systemd gateway, reap orphaned service adapters, and
verify every enabled service + built page came back. There is no manual journal
wipe, no ``ps | awk | kill``, no page re-registration — L1 (pages discovered on
boot) and L2 (service identity re-derived from the filesystem) make the restart
self-heal.

Change detection is signature-based against a small manifest
(``<AWM_DIR>/state/deploy.json``) so a no-op deploy skips the slow pip/npm work;
``--force`` ignores the manifest and does everything.

**Editable installs mean a *code* change needs no reinstall** — the whole tree is
``pip install -e``. Only a change to the *set* of installed dists (a new service
folder, a new component library) requires re-running ``install.sh``. So the
dist-set signature keys on membership, not file content. Page bundles are the
opposite: their built ``dist/`` is what ships, so the page signature keys on
source *content*.

The pure functions here (signatures, ``plan_deploy``, ``missing_from_listing``)
carry the logic and are unit-tested; the CLI command in ``cli.py`` is the thin
imperative shell that runs subprocesses, restarts systemd, and polls the hub.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Root resolution — anchored on the gateway package, never cwd
# ---------------------------------------------------------------------------

def awm_root() -> Path:
    """The ``awm/`` tree the running gateway belongs to — the install / build /
    serve root.

    Anchored on the gateway package (exactly like ``discovery.services_root``),
    never cwd, so ``awm deploy`` always acts on the tree the running ``awm``
    binary was installed from (the editable-install tree = the prod release
    worktree), regardless of where the operator invoked it.
    """
    import awm.gateway
    if awm.gateway.__file__ is not None:
        pkg_dir = Path(awm.gateway.__file__).resolve().parent
    else:
        pkg_dir = Path(next(iter(awm.gateway.__path__))).resolve()
    for parent in pkg_dir.parents:
        if parent.name == "awm" and (parent / "services").is_dir():
            return parent
    # Fixed nesting fallback: <root>/awm/gateway/awm/gateway → parents[2].
    return pkg_dir.parents[2]


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------

def installable_units(root: Path) -> list[str]:
    """The identifiers ``install.sh`` would install: the gateway, every
    component library, and every service folder that ships its own
    ``install.sh``.

    Editable installs mean only *membership* matters (a code edit needs no
    reinstall), so this is a set of names — a newly-added service or component
    changes it, a code change does not.
    """
    units: list[str] = ["gateway"]
    comp = root / "service_components"
    if comp.is_dir():
        for d in sorted(comp.iterdir()):
            if d.is_dir() and not d.name.startswith((".", "_")):
                units.append(f"component:{d.name}")
    svc = root / "services"
    if svc.is_dir():
        for d in sorted(svc.iterdir()):
            if (d / "install.sh").is_file():
                units.append(f"service:{d.name}")
    return sorted(units)


def dist_set_signature(root: Path) -> str:
    """A stable hash of the installable dist *set*. Changes iff a service /
    component is added or removed (never on a code edit)."""
    return hashlib.sha256("\n".join(installable_units(root)).encode()).hexdigest()


def _page_source_files(page_dir: Path):
    """Yield ``(relpath, file)`` for a page's *source* files — everything under
    the page dir except the built ``dist/`` and ``node_modules/``."""
    for p in sorted(page_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(page_dir)
        if rel.parts and rel.parts[0] in ("dist", "node_modules"):
            continue
        yield rel, p


def page_signature(page_dir: Path) -> str:
    """Content hash of a page bundle's source (path + bytes of every source
    file). Changes whenever the page's source changes — the trigger to rebuild
    its ``dist/``."""
    h = hashlib.sha256()
    for rel, p in _page_source_files(page_dir):
        h.update(str(rel).encode())
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()


def page_signatures(root: Path) -> dict[str, str]:
    """``{page_name: content_sig}`` for every *buildable* page (a
    ``pages/<name>/`` with an ``index.html`` — the same marker ``build.sh``
    keys on)."""
    pages = root / "pages"
    out: dict[str, str] = {}
    if not pages.is_dir():
        return out
    for d in sorted(pages.iterdir()):
        if d.is_dir() and (d / "index.html").is_file():
            out[d.name] = page_signature(d)
    return out


# ---------------------------------------------------------------------------
# Manifest — <AWM_DIR>/state/deploy.json
# ---------------------------------------------------------------------------

def manifest_path(awm_dir: Path) -> Path:
    return awm_dir / "state" / "deploy.json"


def load_manifest(awm_dir: Path) -> dict:
    p = manifest_path(awm_dir)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_manifest(awm_dir: Path, data: dict) -> None:
    p = manifest_path(awm_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

@dataclass
class DeployPlan:
    """What a deploy will do, decided from the manifest + current signatures.

    ``do_build`` is all-or-nothing (``build.sh`` rebuilds every buildable page),
    but ``changed_pages`` records *which* pages triggered it, for the operator.
    """
    do_install: bool
    install_reason: str
    do_build: bool
    build_reason: str
    changed_pages: list[str]
    do_reap: bool
    dist_sig: str
    page_sigs: dict[str, str] = field(default_factory=dict)

    def next_manifest(self) -> dict:
        """The manifest to persist *after* a successful deploy — the signatures
        this deploy brought the tree up to."""
        return {"dist_sig": self.dist_sig, "page_sigs": self.page_sigs}


def plan_deploy(
    root: Path,
    awm_dir: Path,
    *,
    force: bool = False,
    no_install: bool = False,
    no_build: bool = False,
    no_reap: bool = False,
) -> DeployPlan:
    """Decide install / build / reap from the prior manifest and current tree.

    Pure (no subprocesses, no network) so it is fully unit-testable. ``--force``
    does everything; the ``--no-*`` flags each veto one step.
    """
    manifest = load_manifest(awm_dir)
    dist_sig = dist_set_signature(root)
    page_sigs = page_signatures(root)
    prev_dist = manifest.get("dist_sig")
    prev_pages = manifest.get("page_sigs", {}) if isinstance(
        manifest.get("page_sigs"), dict) else {}

    # --- install: dist-set membership ---
    if no_install:
        do_install, install_reason = False, "skipped (--no-install)"
    elif force:
        do_install, install_reason = True, "forced (--force)"
    elif prev_dist is None:
        do_install, install_reason = True, "no prior deploy manifest"
    elif prev_dist != dist_sig:
        do_install, install_reason = True, "dist set changed"
    else:
        do_install, install_reason = False, "dist set unchanged"

    # --- build: page-source content ---
    changed_pages = sorted(n for n, s in page_sigs.items()
                           if prev_pages.get(n) != s)
    if no_build:
        do_build, build_reason, changed_pages = False, "skipped (--no-build)", []
    elif force:
        do_build = bool(page_sigs)
        build_reason = "forced (--force)"
        changed_pages = sorted(page_sigs)
    elif changed_pages:
        do_build, build_reason = True, f"{len(changed_pages)} page(s) changed"
    else:
        do_build, build_reason = False, "no page source changed"

    return DeployPlan(
        do_install=do_install,
        install_reason=install_reason,
        do_build=do_build,
        build_reason=build_reason,
        changed_pages=changed_pages,
        do_reap=not no_reap,
        dist_sig=dist_sig,
        page_sigs=page_sigs,
    )


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def missing_from_listing(
    listing: list[dict],
    expected_services: list[str],
    expected_pages: list[str],
) -> dict[str, list[str]]:
    """Given the ``GET /hub/services`` ``services`` array, return the expected
    service / page names that are absent (or, for services, present but not yet
    ``starting``/``ready``).

    A service appears in the listing only once it self-registers over its
    control WS (a few seconds after boot); a page appears the moment L1's
    boot bootstrap registers it. So the verify poll waits on both.
    """
    by_name: dict[str, dict] = {}
    for item in listing:
        name = item.get("name")
        if isinstance(name, str):
            by_name[name] = item

    ok_status = {"starting", "ready"}
    missing_services = [
        n for n in expected_services
        if by_name.get(n) is None
        or by_name[n].get("backend_status") not in ok_status
    ]
    # A page is registered under its folder name (== page_sigs key) as a
    # /ui/<name> base the moment L1 bootstraps it.
    missing_pages = [n for n in expected_pages if by_name.get(n) is None]
    return {"services": missing_services, "pages": missing_pages}

"""Hub adapter for the zotero service — the reference library, mirrored.

Two verbs do the work and they need two different machines. `pull` reads the
Zotero desktop, which lives on one node; `apply` writes the vault, which lives
on another and, on the public host, can never be the first. What crosses
between them is a committed, DVC-pinned bundle in the vault scope — data, moved
the way all data in this workspace moves.

`sync` is the two in order, and is what the timer calls. It is cheap when
nothing has changed: Zotero's library carries a version, and a tick whose
version has not moved costs one HTTP request and stops.

**Who may call what.** Every verb here is operator-only, on the same
discriminator the trilium service uses: `httpsfront` overwrites `X-Awm-As` on
everything it forwards and never forwards an empty one, so an absent identity
means the call came from `/invoke` on loopback. A mirror rewrites a shared
knowledge base; that is not a button on a page.

Run via `run.sh` (which the gateway spawns and respawns):
    python -m awm.zotero.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.zotero import bundle as bundle_mod
from awm.zotero import source, sync, vault

log = logging.getLogger("awm.zotero.hub_adapter")

#: How often the timer looks. Twenty minutes, because the answer is one request
#: when nothing has changed and a person adding a paper is not waiting on it.
INTERVAL_S = float(os.environ.get("ZOTERO_SYNC_INTERVAL_S", "1200"))

#: Whether the timer runs at all. Off on a node that cannot reach the library,
#: where every tick would be a logged failure saying so.
SCHEDULED = os.environ.get("ZOTERO_SYNC_ENABLED", "1") not in ("0", "false", "no")

#: What this node does with the mirror.
#:
#: `full` — read the library and write the vault, which is one machine doing
#: both and is the historical behaviour. `pull` — read the library, ship the
#: bundle, write no vault. `apply` — write the vault from whatever bundle
#: arrives, and never look for a library.
#:
#: The role is what makes the split legible. Without it, the reason a node
#: never pulls is an unreachable host in a log line every twenty minutes.
ROLES = ("full", "pull", "apply")
ROLE = os.environ.get("ZOTERO_ROLE", "full").strip().lower() or "full"
if ROLE not in ROLES:
    # Raised at import, so the service fails to start and the gateway trips its
    # breaker. Visibly wedged is the right answer to a typo that would
    # otherwise quietly turn an apply-only node back into a puller.
    raise SystemExit(f"ZOTERO_ROLE={ROLE!r} is not one of {', '.join(ROLES)}")

#: Where a pulled bundle goes, as `host:/path/to/vault/scope`. Orthogonal to
#: the role: a node can be `full` and still ship, which is what altair does.
SHIP_TO = os.environ.get("ZOTERO_SHIP_TO", "").strip()

#: Whether apply may create the library root when no note carries
#: `#zoteroLibrary`. Off on a shared vault, where creating a library at the top
#: of somebody's tree is worse than refusing.
MAY_CREATE_ROOT = os.environ.get(
    "ZOTERO_MAY_CREATE_ROOT", "1") not in ("0", "false", "no")

API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "tool": "zotero_status",
            "description": (
                "What the mirror holds and where the library is: the bundle's "
                "version, item and file counts, when it was last pulled, and "
                "whether the Zotero desktop is reachable from here."
            ),
            "params": [
                {"name": "probe", "type": "boolean",
                 "description": "Ask the library for its version. Default true."},
            ],
            "timeout": 120,
        },
        {
            "name": "pull",
            "tool": "zotero_pull",
            "description": (
                "Read the Zotero library into the bundle in the vault scope, "
                "fetch any stored file it does not have, and commit the pin. "
                "Free when the library's version has not moved. Operator only, "
                "and it needs the node the library is on."
            ),
            "params": [
                {"name": "force", "type": "boolean",
                 "description": "Re-read even when the version has not moved."},
                {"name": "commit", "type": "boolean",
                 "description": "Pin and commit the result. Default true."},
            ],
            "timeout": 900,
        },
        {
            "name": "apply",
            "tool": "zotero_apply",
            "description": (
                "Write the bundle into the vault: collections as a note tree, "
                "one note per reference with its citation fields as labels, "
                "each stored file attached. Goes under the note carrying "
                "#zoteroLibrary. Only notes carrying #zoteroKey are ever "
                "rewritten. Operator only."
            ),
            "params": [
                {"name": "parent", "type": "string",
                 "description": "Where a fallback library note would be made, "
                                "when no note carries #zoteroLibrary and this "
                                "node may create one. Default root."},
                {"name": "dry_run", "type": "boolean",
                 "description": "Report what would change and write nothing."},
                {"name": "force", "type": "boolean",
                 "description": "Re-apply even when the vault already holds "
                                "this bundle."},
            ],
            "timeout": 3600,
        },
        {
            "name": "ship",
            "tool": "zotero_ship",
            "description": (
                "Send the bundle's library.json to the node that holds the "
                "vault, straight into that node's own vault scope. Only the "
                "JSON travels; stored files stay here. Operator only."
            ),
            "params": [
                {"name": "to", "type": "string",
                 "description": "host:/path/to/vault/scope. Defaults to "
                                "ZOTERO_SHIP_TO."},
            ],
            "timeout": 900,
        },
        {
            "name": "sync",
            "tool": "zotero_sync",
            "description": (
                "Pull, then apply and ship as this node's role says. What the "
                "timer runs, and what to call by hand after adding papers in "
                "Zotero. Operator only."
            ),
            "params": [
                {"name": "force", "type": "boolean",
                 "description": "Pull even when the version has not moved."},
                {"name": "parent", "type": "string",
                 "description": "Where the library note goes. Default root."},
            ],
            "timeout": 3600,
        },
    ],
    "emitters": [],
    "sessions": [],
}

#: What the last tick did, so `status` can answer without running one.
LAST: dict[str, Any] = {"tick": None, "result": None, "error": None}


def _operator_only(as_: str | None, verb: str) -> None:
    """Refuse a verb that arrived through an edge listener.

    The same gate, and the same discriminator, as the trilium service — see
    its `_operator_only`. It is spelled again rather than imported because
    these are separate dists, and the identical three lines are cheaper than
    the coupling.
    """
    if as_ is not None:
        raise PermissionError(
            f"{verb} rewrites the shared vault and is an operator verb: "
            f"run `awm zotero {verb}` on the host")


async def _h_status(args: dict, as_: str | None = None) -> dict:
    out: dict[str, Any] = {"bundle": sync.bundle().stats(),
                           "scope": str(sync.VAULT_SCOPE),
                           "last": dict(LAST)}
    out["role"] = {"role": ROLE, "ship_to": SHIP_TO,
                   "may_create_root": MAY_CREATE_ROOT}
    # An apply-only node has no library to ask. Probing is this verb's default
    # and it is the one zotero verb that is not operator-gated, so on such a
    # node the default would be an ssh to a host that does not resolve — eight
    # seconds of nothing, on every call.
    if ROLE != "apply" and args.get("probe", True) is not False:
        def _probe() -> dict:
            try:
                return {"reachable": True, "versions": source.versions(),
                        "host": source.HOST or "this host",
                        "origin": source.ORIGIN}
            except source.ZoteroUnavailable as e:
                # Not an error state. Zotero is a desktop application and the
                # desktop is sometimes asleep; saying so is the answer.
                return {"reachable": False, "detail": str(e)[:300],
                        "host": source.HOST or "this host",
                        "origin": source.ORIGIN}
        out["library"] = await asyncio.to_thread(_probe)
        if out["library"].get("reachable"):
            # Behind if any library moved, or if one appeared that the bundle
            # has never seen. A library the bundle holds and the desktop no
            # longer offers is not "behind" — `pull` prunes it either way.
            have = out["bundle"]["versions"]
            out["behind"] = any(have.get(lib) != v for lib, v
                                in out["library"]["versions"].items())
    out["scheduled"] = {"enabled": SCHEDULED, "interval_s": INTERVAL_S}
    return out


async def _h_pull(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "pull")
    return await asyncio.to_thread(
        sync.pull, force=bool(args.get("force")),
        commit=args.get("commit", True) is not False)


async def _h_apply(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "apply")
    return await asyncio.to_thread(
        sync.apply, vault.Vault(),
        parent=(args.get("parent") or "").strip() or "root",
        may_create=MAY_CREATE_ROOT,
        force=bool(args.get("force")),
        dry_run=bool(args.get("dry_run")))


async def _h_ship(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "ship")
    to = (args.get("to") or "").strip() or SHIP_TO
    if not to:
        raise ValueError(
            "no ship destination: pass `to`, or set ZOTERO_SHIP_TO to "
            "host:/path/to/vault/scope on the node that holds the vault")
    return await asyncio.to_thread(sync.ship, to)


async def _h_sync(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "sync")
    if ROLE == "apply":
        # No pull, so no bundle change to notice — the far node's own timer is
        # what brings a shipped bundle in.
        return await asyncio.to_thread(
            sync.apply, vault.Vault(),
            parent=(args.get("parent") or "").strip() or "root",
            may_create=MAY_CREATE_ROOT, force=bool(args.get("force")))
    return await asyncio.to_thread(
        sync.run, vault.Vault(), force=bool(args.get("force")),
        parent=(args.get("parent") or "").strip() or "root",
        may_create=MAY_CREATE_ROOT, apply_here=ROLE == "full",
        ship_to=SHIP_TO)


HANDLERS = {
    "status": _h_status,
    "pull": _h_pull,
    "apply": _h_apply,
    "ship": _h_ship,
    "sync": _h_sync,
}


async def _sync_loop() -> None:
    """Sync on a timer. Never exits.

    A *return* from a supervised loop reads as a defect and is respawned, so a
    disabled schedule sleeps through its ticks rather than breaking out. An
    unreachable library is logged at info and waited out: it means the desktop
    is off, which is not something to escalate every twenty minutes.
    """
    log.info("zotero: sync loop started (interval=%ss, enabled=%s)",
             INTERVAL_S, SCHEDULED)
    while True:
        try:
            await asyncio.sleep(INTERVAL_S)
            if not SCHEDULED:
                continue
            LAST["tick"] = asyncio.get_running_loop().time()
            try:
                LAST["result"] = await _h_sync({}, None)
                LAST["error"] = None
            except source.ZoteroUnavailable as e:
                LAST["error"] = str(e)[:300]
                log.info("zotero: library not reachable this tick: %s", e)
        except Exception:  # noqa: BLE001 — never let the loop die
            LAST["error"] = "sync pass failed; see the log"
            log.exception("zotero: sync pass failed")


async def _on_start() -> None:
    """Register, then start the timer.

    Nothing slow happens here. The adapter sends `ready` before this runs and
    the gateway reaps a service that stays unready, so the first sync belongs
    to the loop — a full pull of a large library takes minutes, and doing it
    here would look exactly like a wedged start.
    """
    b = sync.bundle()
    try:
        st = b.stats()
        log.info("zotero: bundle at %s (%s items, versions %s)", b.root,
                 st["items"], st["versions"])
    except Exception:  # noqa: BLE001
        # A bundle that cannot be read is what the first sync is for. Saying so
        # beats refusing to start: the adapter treats an on_start raise as a
        # failed initialisation and the gateway then reaps the service.
        log.warning("zotero: no readable bundle at %s yet", b.root)
    spawn_supervised("zotero:sync", _sync_loop)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("zotero", API_MANIFEST, HANDLERS,
                         on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())

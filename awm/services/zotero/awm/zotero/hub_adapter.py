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
import time
from datetime import datetime, timezone
from typing import Any

from awm.gatewayclient import ServiceAdapter, spawn_supervised

from awm.zotero import bundle as bundle_mod
from awm.zotero import source, stream as stream_mod, sync, vault

log = logging.getLogger("awm.zotero.hub_adapter")

#: How often the timer looks, and it is now a floor rather than the mechanism.
#: The stream is what makes a saved paper appear in seconds; this catches what a
#: stream cannot — a subscription lost behind a healthy socket, a note somebody
#: hand-edited, anything that happened while the service was down.
INTERVAL_S = float(os.environ.get("ZOTERO_SYNC_INTERVAL_S", "1200"))

#: How long to wait after being told a library moved, before reading it.
#:
#: Several libraries can move together and a save is more than one write, so a
#: pass fired on the first frame reads a library mid-change and then has to be
#: told again. Waiting a moment collapses a burst into one pass.
#:
#: Half a second rather than two, because the trade inverted. A second pass used
#: to cost a whole-library read; it now costs one small request, so waiting to
#: avoid one is no longer worth a tenth of the delay a person feels. It is paid
#: on every wake, the lone frame of a single save included.
SETTLE_S = float(os.environ.get("ZOTERO_SETTLE_S", "0.5"))

#: How long to wait before trying again when another pass holds the lock.
BUSY_RETRY_S = float(os.environ.get("ZOTERO_BUSY_RETRY_S", "5"))

#: Whether the timer runs at all. Off on a node that cannot reach the library,
#: where every tick would be a logged failure saying so.
SCHEDULED = os.environ.get("ZOTERO_SYNC_ENABLED", "1") not in ("0", "false", "no")

#: There is no role any more, and its absence is the point. A node used to have
#: to say whether it read a library, wrote a vault, or shipped a bundle between
#: two machines that could not both do it. The library now comes from Zotero's
#: own service, which every node can reach, so one node reads it and writes the
#: vault and there is nothing to choose.

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
                "Read the Zotero library into the bundle in the vault scope "
                "and commit the pin. Free when no library's version has moved. "
                "Operator only."
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
                "Goes under the note carrying "
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
            "name": "sync",
            "tool": "zotero_sync",
            "description": (
                "Read whatever moved in the library and write it into the "
                "vault. What the timer runs as its floor, and what to call by "
                "hand. Operator only."
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

#: What the last pass did, so `status` can answer without running one.
LAST: dict[str, Any] = {"tick": None, "result": None, "error": None,
                        "told": None}


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
    out["may_create_root"] = MAY_CREATE_ROOT
    # An apply-only node has no library to ask. Probing is this verb's default
    # and it is the one zotero verb that is not operator-gated, so on such a
    # node the default would be an ssh to a host that does not resolve — eight
    # seconds of nothing, on every call.
    if args.get("probe", True) is not False:
        def _probe() -> dict:
            try:
                who = source.whoami()
                return {"reachable": True, "versions": source.versions(),
                        "api": source.API, "account": who["username"],
                        # Surfaced because the key sits on a public host and a
                        # write it does not need is worth being able to see.
                        "key_can_write": who["writes"]}
            except source.ZoteroUnavailable as e:
                # Not an error state. The network drops and the service
                # restarts; saying so is the answer.
                return {"reachable": False, "detail": str(e)[:300],
                        "api": source.API}
            except source.ZoteroError as e:
                return {"reachable": False, "detail": str(e)[:300],
                        "api": source.API}
        out["library"] = await asyncio.to_thread(_probe)
        if out["library"].get("reachable"):
            # Behind if any library moved, or if one appeared that the bundle
            # has never seen. A library the bundle holds and the service no
            # longer offers is not "behind" — `pull` prunes it either way.
            have = out["bundle"]["versions"]
            out["behind"] = any(have.get(lib) != v for lib, v
                                in out["library"]["versions"].items())
    out["scheduled"] = {"enabled": SCHEDULED, "interval_s": INTERVAL_S}
    out["stream"] = STREAM.health
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


async def _h_sync(args: dict, as_: str | None = None) -> dict:
    _operator_only(as_, "sync")
    return await asyncio.to_thread(
        sync.run, vault.Vault(), force=bool(args.get("force")),
        parent=(args.get("parent") or "").strip() or "root",
        may_create=MAY_CREATE_ROOT,
        # What set this off, so the status note can say. A note whose whole job
        # is to tell you what happened must not guess at the half it knows.
        trigger=(args.get("trigger") or "hand"))


HANDLERS = {
    "status": _h_status,
    "pull": _h_pull,
    "apply": _h_apply,
    "sync": _h_sync,
}


#: Set when the stream says a library moved. An event rather than a queue: what
#: matters is that something changed, not how many times, and a pass reads
#: whatever has moved by the time it runs.
WOKEN = asyncio.Event()

STREAM = stream_mod.Stream(
    lambda library, version: _woken(library, version))


async def _woken(library: str, version: int) -> None:
    """What the stream calls. Deliberately does almost nothing.

    The stream must keep reading its socket while a pass runs, or a change that
    lands during a long apply is never delivered. So this sets a flag and
    returns, and the worker below does the work.

    The two stamps are for two different questions. `at` is a wall clock to a
    millisecond, and is what a save's own upload time is compared against to
    price the leg between Zotero accepting an upload and this frame arriving —
    the one segment of the delay nothing here can shorten. `since` is monotonic
    and prices the leg the push loop owns.
    """
    LAST["told"] = {"library": library, "version": version,
                    "at": datetime.now(timezone.utc)
                    .isoformat(timespec="milliseconds"),
                    "since": time.monotonic()}
    WOKEN.set()


async def _push_loop() -> None:
    """Sync when the stream says to. Never exits.

    One pass at a time, and the flag is cleared *before* the pass rather than
    after: a change arriving while a pass is running must leave the flag set, so
    the next turn of this loop picks it up instead of the pass swallowing it.
    """
    log.info("zotero: push loop started (settle=%ss)", SETTLE_S)
    while True:
        try:
            await WOKEN.wait()
            await asyncio.sleep(SETTLE_S)
            WOKEN.clear()
            LAST["tick"] = "push"
            told_at = (LAST.get("told") or {}).get("since")
            try:
                LAST["result"] = await _h_sync({"trigger": "push"}, None)
                LAST["error"] = None
                if told_at is not None:
                    # The frame-to-note number, which is what a person feels.
                    # The pass logs its own breakdown; this is the wrapper
                    # around it, settle included.
                    log.info("zotero: %.1fs from the stream frame to the pass "
                             "finishing", time.monotonic() - told_at)
            except source.ZoteroUnavailable as e:
                LAST["error"] = str(e)[:300]
                log.info("zotero: library not reachable on this push: %s", e)
            except sync.Busy:
                # Not "it will pick this up anyway". The pass holding the lock
                # may have read its libraries *before* this frame arrived, and
                # the flag was cleared above, so dropping it here leaves the
                # paper for the floor tick twenty minutes away. Put the flag
                # back and wait for the other pass to let go.
                log.info("zotero: a sync was already running; retrying in %ss",
                         BUSY_RETRY_S)
                WOKEN.set()
                await asyncio.sleep(BUSY_RETRY_S)
        except Exception:  # noqa: BLE001 — never let the loop die
            LAST["error"] = "push pass failed; see the log"
            log.exception("zotero: push pass failed")
            await asyncio.sleep(5)


async def _sync_loop() -> None:
    """Sync on a timer. Never exits.

    A *return* from a supervised loop reads as a defect and is respawned, so a
    disabled schedule sleeps through its ticks rather than breaking out. An
    unreachable library is logged at info and waited out: it means the desktop
    is off, which is not something to escalate every twenty minutes.
    """
    log.info("zotero: floor loop started (interval=%ss, enabled=%s)",
             INTERVAL_S, SCHEDULED)
    while True:
        try:
            await asyncio.sleep(INTERVAL_S)
            if not SCHEDULED:
                continue
            LAST["tick"] = "floor"
            try:
                LAST["result"] = await _h_sync({"trigger": "floor"}, None)
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
    spawn_supervised("zotero:push", _push_loop)
    spawn_supervised("zotero:stream", STREAM.run)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter("zotero", API_MANIFEST, HANDLERS,
                         on_start=_on_start).run()


if __name__ == "__main__":
    asyncio.run(main())

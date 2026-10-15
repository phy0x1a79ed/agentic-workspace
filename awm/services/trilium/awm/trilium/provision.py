"""Give the vault a database, so nobody's first visit is a setup wizard.

A fresh Trilium serves a setup screen and waits. That was right when the wizard
was also where a person chose their password — it was the one step awm could
not take for them. With the edge as the only way in there is no password to
choose, so the wizard asks nothing and the right first visit is a working
knowledge base.

**Why this is safe to call on a loop.** `GET /api/setup/status` is
unauthenticated and cheap, so the probe is the idempotency check and there is no
flag to keep in step with the filesystem. Trilium guards the creation endpoint
with `checkAppNotInitialized` on its own side, so a second attempt — two
supervision ticks, or a tick racing a `start` — is refused by the server rather
than doing anything twice. Both of those are upstream's checks, not ours, which
is why this module holds no state.

**Why it needs no credential.** The creation endpoint's other guard is
`checkSetupAuth`, which stands down when Trilium's own authentication is off.
That is the same setting `server.child_env` sets, and this call goes over
loopback to a child that only the edge can reach.

Provisioning also settles one thing about the launchbar — see
:func:`hide_day_note_launchers`. It is here rather than in a config file
because Trilium keeps the launchbar *in the database*, so the only way to say
"not this one" is to move a note.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from awm import config

log = logging.getLogger("awm.trilium.provision")

#: Short: this runs inside a supervision tick, and a hung vault must not stall
#: the loop that would otherwise report it.
TIMEOUT_S = 10.0


def _request(path: str, *, method: str = "GET",
             body: dict | None = None, timeout: float = TIMEOUT_S) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{config.VAULT_URL}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def status() -> dict[str, Any]:
    """Trilium's own account of whether it has a database yet."""
    return _request("/api/setup/status")


def ensure_document(*, demo: bool = False) -> dict[str, Any]:
    """Create an empty knowledge base if there is none.

    Returns what happened and never raises for a vault that is merely not
    ready: a child still binding its port is the normal case on a cold start,
    and the supervision loop will be back in twenty seconds.

    `demo=False` skips the sample notes upstream ships. They are a tour of the
    features, and a shared vault that starts with somebody's demo content in it
    is a tidy-up nobody volunteered for.
    """
    try:
        st = status()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"action": "unreachable", "initialized": False,
                "detail": f"{type(exc).__name__}: {exc}"}

    if st.get("isInitialized"):
        return {"action": "already-initialized", "initialized": True}

    # A vault whose schema exists but is not initialized is mid-sync, which is
    # a state a human chose. Creating a document over it would be destructive.
    if st.get("schemaExists"):
        return {"action": "sync-in-progress", "initialized": False,
                "detail": "schema exists but the vault is not initialized"}

    suffix = "" if demo else "?skipDemoDb"
    try:
        _request(f"/api/setup/new-document{suffix}", method="POST",
                 body={"locale": "en"}, timeout=60.0)
    except urllib.error.HTTPError as exc:
        # `checkAppNotInitialized` refusing means somebody else won the race,
        # which is a success for our purposes. Anything else is reportable.
        detail = exc.read().decode("utf-8", "replace")[:200]
        if exc.code in (400, 409):
            log.info("trilium: provisioning already under way (%s)", exc.code)
            return {"action": "already-under-way", "initialized": True,
                    "detail": detail}
        return {"action": "failed", "initialized": False,
                "detail": f"HTTP {exc.code}: {detail}"}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"action": "failed", "initialized": False,
                "detail": f"{type(exc).__name__}: {exc}"}

    log.info("trilium: created an empty vault at %s", config.VAULT_URL)
    return {"action": "created", "initialized": True}


# -- the launchbar ----------------------------------------------------------

#: ``(launcher, on the bar, off the bar)``. Trilium's launchbar is a pair of
#: note lists in the hidden subtree, and "hidden" here means moved from the
#: first to the second. The IDs are fixed by upstream's rule that the hidden
#: subtree has a predictable structure, so nothing has to be looked up.
DAY_NOTE_LAUNCHERS = (
    ("_lbToday", "_lbVisibleLaunchers", "_lbAvailableLaunchers"),
    ("_lbCalendar", "_lbVisibleLaunchers", "_lbAvailableLaunchers"),
    ("_lbMobileCalendar", "_lbMobileVisibleLaunchers",
     "_lbMobileAvailableLaunchers"),
)


def hide_day_note_launchers() -> dict[str, Any]:
    """Take the day-note buttons off the launchbar, on every start.

    Trilium's "Today" launcher and its calendar widget both call `getDayNote`,
    which *creates* `Calendar / <year> / <month> / <date>` on first click and
    leaves it there. In a personal vault that is a feature; in one shared by
    everybody it is a dated folder tree that appears in everyone's note list
    because one person once looked at a calendar. Nothing else on this
    deployment reaches `getDayNote` — the command has no default shortcut — so
    moving these two off the bar is the whole of the fix.

    Moving rather than deleting, because upstream recreates a launcher whose
    *note* has gone, under the parent its definition names. It does not
    recreate a branch: `checkHiddenSubtree` enforces branch placement only for
    items marked `enforceBranches`, which no launcher is, and comments that
    launchers moving between visible and available change branch by design.
    So the move is what survives a restart, and a delete is what would not.

    Re-applied on every provision, which is once per service start. Somebody
    who deliberately puts the Today button back will find it gone again next
    start; that is enforcement, and it is deliberate — the tree it creates is
    shared, and taking it out again is not a tidy-up anyone volunteered for.

    Never raises. A vault that cannot be reached is the cold-start case, and a
    launchbar is not worth failing a provision over.
    """
    from awm.trilium import etapi as _etapi

    api = _etapi.client()
    moved, failed = [], []
    for note_id, visible, available in DAY_NOTE_LAUNCHERS:
        try:
            # Create before delete: a note's last branch takes the note with it.
            api.put_branch(note_id, available)
            api.delete_branch(api.branch_id(note_id, visible))
        except _etapi.EtapiError as exc:
            failed.append(f"{note_id}: {exc}")
        else:
            moved.append(note_id)
    if failed:
        log.warning("trilium: could not hide the day-note launchers: %s",
                    "; ".join(failed))
    return {"moved": moved, "failed": failed}

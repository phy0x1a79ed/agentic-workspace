"""Hub adapter for the compute service — the local-compute watchdog.

Boots the watchdog on the shared ``awm.gatewayclient.ServiceAdapter`` loop
(register → ready → serve → reconnect). The gateway injects ``AWM_HUB_URL`` /
``AWM_SERVICE_NAME`` / ``AWM_SERVICE_ID``; there is no auth.

What this service is for: agents on this box launch local compute — builds,
test suites, embedding runs, media encodes — with no sense of what else is
running, and one oversized job can wedge a shared machine for eleven live
sessions at once. The watchdog leaves agents free, intervenes only when the box
is genuinely at risk, and tells the responsible agent what happened and that
the work probably belonged on remote compute.

The sampling loop is launched from ``on_start`` as a background task, exactly
like the fileviewer's static mount: it never returns, so awaiting it would stop
the adapter ever serving its control WS.

``grant`` is CLI/HTTP only — deliberately off the MCP surface. It takes the
caller's session id, which an agent supplies from its own shell as
``$CLAUDE_CODE_SESSION_ID``; over MCP there is no shell to expand it and no
honest way to know who is asking.

Run via ``run.sh`` (which the gateway spawns and respawns):
    python -m awm.compute.hub_adapter
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.compute import notices, store
from awm.compute.watcher import Watcher

log = logging.getLogger("awm.compute.hub_adapter")

WATCHER = Watcher()


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "status",
            "tool": "compute_status",
            "description": (
                "Report the local-compute watchdog: box size, the derived caps "
                "(hard ceiling = nproc-2 cores and total-minus-reserve memory, "
                "always live; soft cap = half the box, live only under "
                "pressure), current box pressure and gate state, whether the "
                "watchdog is armed or observing, and its own measured duty "
                "cycle. Read this first to understand why a job was or was not "
                "acted on."
            ),
            "params": [],
        },
        {
            "name": "sessions",
            "tool": "compute_sessions",
            "description": (
                "Live per-agent-session footprint: how many processes each "
                "Claude Code session owns, the CPU cores it is currently "
                "burning (including reaped children, so parallel builds are "
                "visible), its memory estimate, and its job roots. Memory here "
                "is a SCREENING estimate that double-counts shared pages and "
                "runs 40%-1200% high; only the accurate read taken at decision "
                "time can trigger an action. Also lists any open violations."
            ),
            "params": [],
        },
        {
            "name": "explain",
            "tool": "compute_explain",
            "description": (
                "Explain one process: which agent session owns it and how that "
                "was determined, whether it is protected from ever being acted "
                "on (claude harness, MCP server, gateway, awm service, dev "
                "sandbox), whether it is a job root, its ancestor chain with "
                "per-process attribution, and its owning session's usage. The "
                "debugging tool for 'why was/wasn't this touched'."
            ),
            "params": [{"name": "pid", "type": "integer", "required": True}],
        },
        {
            "name": "decisions",
            "tool": "compute_decisions",
            "description": (
                "The watchdog's ledger, newest first — every judgement it "
                "reached, including the ones it dropped after an accurate "
                "re-measurement, the ones it refused on a safety assertion, and "
                "the ones dry-run suppressed. Each row carries the metric, the "
                "measured value, the cap, the command line, and the box state "
                "at that moment."
            ),
            "params": [{"name": "limit", "type": "integer", "required": False}],
        },
        {
            "name": "arm",
            "tool": "compute_arm",
            "description": (
                "Set the watchdog's posture: 'observe' (judges and records, "
                "clearly not enforcing), 'shadow' (records exactly what it "
                "WOULD do and signals nothing — run this before trusting it), "
                "or 'live' (acts). Pass nothing to read the current posture. "
                "Going back to observe or shadow is the instant rollback, no "
                "restart needed. 'live' only takes effect on the production "
                "gateway; a dev sandbox's copy stays observation-only "
                "regardless, so starting a sandbox cannot put a second "
                "watchdog on the same processes."
            ),
            "params": [
                {"name": "mode", "type": "string", "required": False},
            ],
        },
        {
            "name": "tune",
            "tool": "compute_tune",
            "description": (
                "Read or set thresholds at runtime — cpu_headroom_cores, "
                "mem_reserve_gb, soft_fraction, mem_avail_floor_gb, dwell_mem_s, "
                "dwell_cpu_s, quiet_period_s and the rest. Pass nothing to read "
                "the current values. Every threshold is derived from box size "
                "by default; setting one overrides that derivation until reset."
            ),
            "params": [
                {"name": "values", "type": "object", "required": False},
                {"name": "reset", "type": "boolean", "required": False},
            ],
        },
        {
            "name": "grant",
            "tool": "compute_grant",
            "description": (
                "Take a bounded exemption for a session that legitimately needs "
                "more of the box than the cap allows. Size-bounded, "
                "time-bounded (max 1 hour), reason mandatory, logged. Pass your "
                "own session id — from an agent's shell that is "
                "$CLAUDE_CODE_SESSION_ID. CLI/HTTP only, by design."
            ),
            "params": [
                {"name": "session", "type": "string", "required": True},
                {"name": "reason", "type": "string", "required": True},
                {"name": "mem_gb", "type": "number", "required": False},
                {"name": "cpu_cores", "type": "number", "required": False},
                {"name": "ttl_min", "type": "number", "required": False},
            ],
            "surfaces": ["cli", "http"],
        },
        {
            "name": "grants",
            "tool": "compute_grants",
            "description": "List every live exemption: session, size, reason, expiry.",
            "params": [],
        },
        {
            "name": "revoke",
            "tool": "compute_revoke",
            "description": "Revoke a session's live exemptions immediately.",
            "params": [{"name": "session", "type": "string", "required": True}],
            "surfaces": ["cli", "http"],
        },
    ],
    "emitters": [],
    "sessions": [],
}


# -- function handlers ------------------------------------------------------


def _h_status(_args: dict) -> dict:
    return WATCHER.status()


def _h_sessions(_args: dict) -> dict:
    return WATCHER.dump()


def _h_explain(args: dict) -> dict:
    return WATCHER.explain(int(args["pid"]))


def _h_decisions(args: dict) -> dict:
    return {"decisions": store.recent_decisions(int(args.get("limit") or 50))}


# One string beats two booleans here: the CLI generator renders a boolean
# parameter as a bare flag, which can only ever turn a setting ON — leaving no
# way to roll back from the command line, which is the one direction that has
# to work under pressure.
MODES = {
    "observe": {"armed": False, "dry_run": True},
    "shadow": {"armed": True, "dry_run": True},
    "live": {"armed": True, "dry_run": False},
}


def _current_mode() -> str:
    if not WATCHER.armed:
        return "observe"
    return "shadow" if WATCHER.dry_run else "live"


def _h_arm(args: dict) -> dict:
    mode = (args.get("mode") or "").strip().lower()
    if mode:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; one of {sorted(MODES)}")
        store.set_settings(MODES[mode])
        WATCHER.reload_settings()
        log.info("compute watchdog posture -> %s (effective: %s)",
                 mode, _current_mode())
    return {
        "mode": _current_mode(),
        "requested": mode or None,
        "armed": WATCHER.armed,
        "dry_run": WATCHER.dry_run,
        "arm_eligible": WATCHER.arm_eligible,
        "note": (
            "" if WATCHER.arm_eligible else
            "this gateway is not the production origin — the watchdog stays "
            "observation-only here no matter what is set"
        ),
    }


def _h_tune(args: dict) -> dict:
    from dataclasses import fields

    from awm.compute.policy import Thresholds

    known = {f.name for f in fields(Thresholds)}
    if args.get("reset"):
        store.clear_settings(sorted(known))
        WATCHER.reload_settings()
    values = {k: v for k, v in (args.get("values") or {}).items() if k in known}
    unknown = sorted(set(args.get("values") or {}) - known)
    if values:
        store.set_settings(values)
        WATCHER.reload_settings()
    if unknown:
        raise ValueError(f"unknown thresholds: {unknown}; known: {sorted(known)}")
    return {
        "thresholds": WATCHER.judge.thresholds.as_dict() if WATCHER.judge else {},
        "caps": WATCHER.judge.caps.as_dict() if WATCHER.judge else {},
    }


MAX_GRANT_TTL_MIN = 60.0


def _h_grant(args: dict) -> dict:
    session = str(args["session"]).strip()
    reason = str(args["reason"]).strip()
    if not session or not reason:
        raise ValueError("session and reason are both required")
    ttl_min = min(float(args.get("ttl_min") or 30.0), MAX_GRANT_TTL_MIN)
    mem_gb = args.get("mem_gb")
    cpu_cores = args.get("cpu_cores")
    if mem_gb is None and cpu_cores is None:
        raise ValueError("give at least one of mem_gb / cpu_cores")
    row = store.add_grant(
        session,
        reason=reason,
        mem_gb=float(mem_gb) if mem_gb is not None else None,
        cpu_cores=float(cpu_cores) if cpu_cores is not None else None,
        ttl_s=ttl_min * 60.0,
    )
    log.info("compute grant: %s mem=%s cpu=%s for %.0f min (%s)",
             session[:8], mem_gb, cpu_cores, ttl_min, reason)
    return {"grant": row}


def _h_grants(_args: dict) -> dict:
    return {"grants": list(store.active_grants().values())}


def _h_revoke(args: dict) -> dict:
    return {"revoked": store.revoke_grants(str(args["session"]))}


HANDLERS = {
    "status": _h_status,
    "sessions": _h_sessions,
    "explain": _h_explain,
    "decisions": _h_decisions,
    "arm": _h_arm,
    "tune": _h_tune,
    "grant": _h_grant,
    "grants": _h_grants,
    "revoke": _h_revoke,
}


# -- startup orchestration --------------------------------------------------


def _on_start() -> None:
    """Launch the sampling loop as a task — it never returns."""
    asyncio.create_task(WATCHER.run())
    log.info("compute watchdog loop launched (notices → %s)", notices.notice_root())


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    await ServiceAdapter(
        "compute",
        API_MANIFEST,
        HANDLERS,
        on_start=_on_start,
    ).run()


if __name__ == "__main__":
    asyncio.run(main())

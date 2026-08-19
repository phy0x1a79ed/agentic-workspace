"""
Best-effort bridge to the turn-end-open-items Stop hook's arm/disarm marker.

That hook (~/.claude/hooks/turn-end-open-items.py, not part of this repo) can
block a session's Stop event when the session is "armed" and has open todos or
background jobs. Reflection queues slash commands (e.g. /compact) behind a
caller's current turn and waits for that turn's Stop event to fire so the
queued command can run — if the nag blocks that Stop, the wait stalls behind a
hook reflection has no visibility into. So reflection disarms the calling
session's gate before queuing such a wait, and rearms it once the wait ends.

Deliberate soft coupling to an external file convention (empty file at
~/.claude/stop-gate/<session_id> means armed) rather than importing the hook's
module, since it lives outside this repo. Keep GATE_DIR and the file layout in
sync with that hook by convention.

Failures here must never block delivery — every function swallows OSError.
"""
import logging
import os

logger = logging.getLogger(__name__)

GATE_DIR = os.path.expanduser("~/.claude/stop-gate")


def _gate_path(session_id):
    return os.path.join(GATE_DIR, session_id) if session_id else None


def disarm(session_id: str) -> None:
    path = _gate_path(session_id)
    if not path:
        return
    try:
        os.remove(path)
    except OSError as exc:
        if getattr(exc, "errno", None) != 2:  # ENOENT: not armed, fine
            logger.warning("stop_gate.disarm(%s) failed: %s", session_id, exc)


def rearm(session_id: str) -> None:
    path = _gate_path(session_id)
    if not path:
        return
    try:
        os.makedirs(GATE_DIR, exist_ok=True)
        with open(path, "w"):
            pass
    except OSError as exc:
        logger.warning("stop_gate.rearm(%s) failed: %s", session_id, exc)

"""What reflection refuses to inject, and the resume it enforces.

These rules are about the *agent TUI*, not about how a keystroke reaches it, so
they live apart from any backend: a tmux-hosted session and a background one
present the same modal dialogs and lose context to the same commands, and it
would be a nasty surprise if `/clear` were guarded on one path and not the other.
"""
from __future__ import annotations

import re
from typing import Optional

# Slash commands that irreversibly discard context / end the session. Refused
# unless the caller passes confirm=true.
DESTRUCTIVE = {"/clear", "/quit", "/exit"}

# Slash commands that open a blocking modal / picker in the TUI (a settings pane,
# a navigable list, …). Pasted input and Enter are SWALLOWED by the modal — a
# follow-up prompt does not escape it, and for a navigable list an Enter drills
# deeper — so reflection cannot drive them and would leave the session frozen
# (only a hand-typed Escape recovers). Refused outright; run these by hand.
# Empirically verified for /status and /mcp (both trap input; Esc-only exit).
# Best-effort curated list — extend as new modal commands appear.
INTERACTIVE = {
    "/mcp", "/status", "/config", "/permissions", "/agents",
    "/hooks", "/resume", "/theme", "/login", "/logout",
    "/bashes", "/doctor", "/statusline", "/vim",
    "/rc", "/remote-control",
}

# Commands that act directly WITH an argument but open a picker modal when bare
# (e.g. `/model opus` switches immediately; `/model` alone opens the chooser).
_MODAL_WHEN_BARE = {"/model"}

# A bare slash command (e.g. /compact) runs at end-of-turn and then leaves the
# session idle with nothing to do — for an autonomous agent that is death. So a
# self-directed slash command must be trailed by a real prompt that gives the
# session a next turn once the command completes. This is the default when the
# caller does not supply their own `followup`.
DEFAULT_FOLLOWUP = "Continue with what you were doing."


def is_slash(text: str) -> bool:
    """True if ``text`` is a slash command (a TUI control command, not a prompt)."""
    return text.strip().startswith("/")


def opens_modal(text: str) -> bool:
    """True if ``text`` opens a blocking modal/picker that traps pasted input.

    Covers the curated :data:`INTERACTIVE` set plus arg-less forms in
    :data:`_MODAL_WHEN_BARE` (``/model`` alone opens a chooser; ``/model opus``
    acts directly).
    """
    parts = text.strip().split()
    first = parts[0]
    if first in INTERACTIVE:
        return True
    if first in _MODAL_WHEN_BARE and len(parts) == 1:
        return True
    return False


def refusal(text: str, *, confirm: bool,
            allow_modal: bool = False) -> Optional[dict]:
    """Return the refusal result for ``text``, or ``None`` if it may be injected.

    ``allow_modal`` is for a verb that drives the modal it opens (the
    Remote Control verbs); a free-text caller never sets it.

    Raises ``ValueError`` for empty text — that is a caller bug, not a refusal.
    """
    if not text or not text.strip():
        raise ValueError("text is required")
    first = text.strip().split()[0]
    if first in DESTRUCTIVE and not confirm:
        return {
            "ok": False,
            "refused": True,
            "reason": f"{first!r} irreversibly discards context; "
                      f"pass confirm=true to proceed.",
            "guard": sorted(DESTRUCTIVE),
        }
    if not allow_modal and opens_modal(text):
        return {
            "ok": False,
            "refused": True,
            "kind": "interactive",
            "reason": f"{first!r} opens an interactive modal/picker that "
                      f"swallows pasted input; reflection cannot navigate it and "
                      f"it would freeze the session (only a hand-typed Esc "
                      f"recovers). Run it by hand.",
            "guard": sorted(INTERACTIVE),
        }
    return None


def resume_text(followup: Optional[str]) -> str:
    """The prompt to inject once a slash command completes."""
    return followup.strip() if followup and followup.strip() else DEFAULT_FOLLOWUP


# Copy a TUI modal or permission prompt paints that the ordinary prompt screen
# does not. TUI copy can move under a CLI update; a stale list fails toward
# "no modal seen", which is why the session record's `waiting` status is asked
# as well before anything is typed.
_MODAL_MARKERS = (
    "esc to cancel", "enter to confirm", "enter to select", "to navigate",
    "do you want to", "disconnect this session",
)
_SCREEN_TAIL_CHARS = 2500
_CSI_MOVE = re.compile(r"\x1b\[[0-9;?]*[ABCDEFGHJKfd]")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]")


def plain_screen(screen: str) -> str:
    """A screen as text. A cursor move becomes a line break, other escapes go.

    The daemon lane hands back the pty byte stream, where a repaint of one line
    is a cursor move followed by that line's text.
    """
    return _ANSI.sub("", _CSI_MOVE.sub("\n", screen))


def modal_on_screen(screen: str) -> Optional[str]:
    """The marker that shows a modal or permission prompt on ``screen``, or None."""
    tail = plain_screen(screen)[-_SCREEN_TAIL_CHARS:].lower()
    return next((m for m in _MODAL_MARKERS if m in tail), None)

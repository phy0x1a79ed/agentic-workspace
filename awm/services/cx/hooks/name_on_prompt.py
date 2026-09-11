#!/usr/bin/env python3
"""Hand a claimed cx session back to Claude Code's own namer, on its first prompt.

Claude Code titles a background session by itself. A side query turns the user's
request into a two-to-four word label and writes it with `nameSource: "auto"`.
It runs only when the job's state record carries no name and an intent, and a
session out of the cx pool has neither in the shape the namer needs: the pool
names it at launch, and the intent is captured once at dispatch from a prompt
that a pool session, launched empty, never receives.

So this hook supplies both. It takes the pool's `claimed <noun>` off the record
and writes the prompt in as the intent, and the classifier titles the session at
the end of that turn.

Runs on every prompt in every session on the node. It does nothing at all unless
the record still reads `claimed <noun>`, which only a cx claim writes, and it
never costs a prompt: every path exits 0 and prints nothing.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

#: Mirrors `awm.cx.config.claimed_prefix`. Not imported from it: this runs
#: inside the user's own Claude Code session, which has no awm environment.
PREFIX = os.environ.get("AWM_CX_CLAIMED_PREFIX") or "claimed "

#: What the vendor truncates an intent to.
INTENT_MAX = 500


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return
    if not isinstance(payload, dict):
        return
    # `source` separates a person at the composer from a wake-up, a schedule or
    # an SDK caller. Only a person's first sentence should title the session.
    if (payload.get("source") or "user") != "user":
        return
    prompt = _spoken(payload.get("prompt"))
    # A slash command is not a request. Claude Code's own intent capture skips
    # them too, which is why `/cd` never titled a claimed session.
    if not prompt or prompt.startswith("/"):
        return
    job = os.environ.get("CLAUDE_JOB_DIR")
    if not job:
        return
    _clear(os.path.join(job, "state.json"), prompt[:INTENT_MAX])


def _spoken(prompt: object) -> str:
    """The part of the prompt the user typed, without injected context."""
    if not isinstance(prompt, str):
        return ""
    tag = "</system-reminder>"
    cut = prompt.rfind(tag)
    return (prompt[cut + len(tag):] if cut >= 0 else prompt).strip()


def _clear(path: str, intent: str) -> None:
    """Drop the name and record the intent, or leave the file exactly as it is.

    CAUTION: read, modify, write, with no lock — the session process writes this
    same file. The window is one hook's worth of work and the loser is the
    session's own concurrent update, so keep everything between the read and the
    replace to the two keys below.
    """
    try:
        with open(path, "rb") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return
    if not isinstance(state, dict):
        return
    if not str(state.get("name") or "").startswith(PREFIX):
        return
    state.pop("name", None)
    state.pop("nameSource", None)
    # `??`, not `||`, is what merges this field back on every later write, so an
    # empty string would survive the whole session and the namer would never run.
    state["intent"] = intent
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except OSError:
        return


if __name__ == "__main__":
    try:
        main()
    except Exception:  # a hook must never cost the user a prompt
        pass

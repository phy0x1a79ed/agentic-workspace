"""Would a session working in this directory ask the user a question first?

`/cd` into a directory the session has not worked in before opens a trust
dialog — "This session hasn't worked here before. Is this a directory you
created or one you trust?" — whose default answer is "No, stay put". A session
sitting on that dialog has not moved and is not idle, and the next thing typed
into it answers the dialog instead of doing what it meant to. A session started
in an untrusted directory stops at the same dialog before it does any work.

So a caller asks first and declines when the answer would be a question. The
user answers the trust prompt themselves in their own terminal, which is where
a trust decision belongs.

Trust is recorded per directory in `~/.claude.json` and inherited from any
trusted ancestor, which is why a fresh directory under a trusted home moves
without a word while `/tmp` does not. A shape this file no longer has reads as
untrusted, so a Claude Code update that moves it costs the warm start and
nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path


def default_trust_file() -> Path:
    return Path.home() / ".claude.json"


def trusted(path: str | Path, *, trust_file: Path | None = None) -> bool:
    """True if `/cd path` would move without opening the trust dialog."""
    try:
        with open(trust_file or default_trust_file(), "rb") as fh:
            projects = (json.load(fh) or {}).get("projects")
    except (OSError, ValueError, AttributeError):
        return False
    if not isinstance(projects, dict):
        return False
    p = Path(path).resolve()
    for cand in (p, *p.parents):
        entry = projects.get(str(cand))
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted"):
            return True
    return False

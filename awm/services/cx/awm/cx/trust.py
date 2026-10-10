"""Would moving a session to this directory ask the user a question first?

`/cd` into a directory the session has not worked in before opens a trust
dialog — "This session hasn't worked here before. Is this a directory you
created or one you trust?" — whose default answer is "No, stay put". A session
sitting on that dialog has not moved and is not idle, and the next thing typed
into it answers the dialog instead of doing what it meant to.

So the pool asks first and declines the claim when the answer would be a
question. The caller then launches cold, and the user answers the trust prompt
themselves in their own terminal, which is where a trust decision belongs.

Trust is recorded per directory in `~/.claude.json` and inherited from any
trusted ancestor, which is why a fresh directory under a trusted home moves
without a word while `/tmp` does not. A shape this file no longer has reads as
untrusted, so a Claude Code update that moves it costs the warm start and
nothing else.
"""

from __future__ import annotations

import os
from pathlib import Path

from awm.claudedaemon import trust as _shared


def trust_file() -> Path:
    return Path(os.environ.get("AWM_CX_TRUST_FILE") or _shared.default_trust_file())


def trusted(path: str | Path) -> bool:
    """True if `/cd path` would move without opening the trust dialog.

    The check is `awm.claudedaemon.trust.trusted`, aimed at cx's trust file.
    """
    return _shared.trusted(path, trust_file=trust_file())

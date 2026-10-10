"""Would a session working in this directory ask the user a question first?

Two questions, because Claude Code applies two different rules.

`trusted` answers for `/cd` into a directory the session has not worked in
before. That opens a trust dialog — "This session hasn't worked here before. Is
this a directory you created or one you trust?" — whose default answer is "No,
stay put". A session sitting on that dialog has not moved and is not idle, and
the next thing typed into it answers the dialog instead of doing what it meant
to. Trust is recorded per directory in `~/.claude.json` and inherited from any
trusted ancestor, which is why a fresh directory under a trusted home moves
without a word while `/tmp` does not.

`start_refusal` answers for `claude --bg` starting in a directory. There the
rule is stricter: inside a git repository the workspace-trust entry must be the
repository's own, and an ancestor such as the home directory does not stand in
for it. For a linked worktree the entry is the main repository, which here is
the `.bare` directory; for a plain repository it is the repository root. Only a
directory outside git inherits from an ancestor. A start that fails this check
does not error: the daemon refuses with "Workspace not trusted", and the caller
sees nothing until its wait for a new job times out.

The user answers the trust prompt themselves in their own terminal, which is
where a trust decision belongs. A shape of `~/.claude.json` this module no
longer recognises reads as untrusted, so a Claude Code update that moves it
costs the start and nothing else.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def default_trust_file() -> Path:
    return Path.home() / ".claude.json"


def _accepted(trust_file: Path | None) -> dict | None:
    """The projects that accepted the trust dialog, or None if unreadable."""
    try:
        with open(trust_file or default_trust_file(), "rb") as fh:
            projects = (json.load(fh) or {}).get("projects")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(projects, dict):
        return None
    return {k: v for k, v in projects.items()
            if isinstance(v, dict) and v.get("hasTrustDialogAccepted")}


def trusted(path: str | Path, *, trust_file: Path | None = None) -> bool:
    """True if `/cd path` would move without opening the trust dialog."""
    accepted = _accepted(trust_file)
    if accepted is None:
        return False
    p = Path(path).resolve()
    return any(str(cand) in accepted for cand in (p, *p.parents))


def workspace_key(path: str | Path) -> Path | None:
    """The git repository Claude Code keys workspace trust on, or None.

    Resolved through the common git directory, so every worktree of a
    repository maps to the one entry: the repository root for an ordinary
    repository (the parent of its `.git`), and the bare directory itself for a
    repository whose worktrees hang off a bare clone. None means ``path`` is
    not inside a git repository.
    """
    # An inherited GIT_DIR, GIT_WORK_TREE, GIT_COMMON_DIR or GIT_INDEX_FILE
    # overrides `-C` and would answer for the service's repository, not ``path``.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute",
             "--git-common-dir"],
            capture_output=True, text=True, timeout=5, check=False, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    line = out.stdout.strip()
    if out.returncode != 0 or not line:
        return None
    common = Path(line).resolve()
    return common.parent if common.name == ".git" else common


def start_refusal(path: str | Path, *, trust_file: Path | None = None) -> str | None:
    """Why `claude --bg` would refuse to start in ``path``, or None if it would not.

    The message names the exact entry that needs trust and how to grant it.
    """
    accepted = _accepted(trust_file)
    key = workspace_key(path)
    if key is not None:
        if accepted is not None and str(key) in accepted:
            return None
        where = (f"{path} is a git worktree of {key}, and Claude Code keys its "
                 f"trust on that repository, not on a parent directory"
                 if Path(path).resolve() != key else
                 f"{key} is a git repository, and Claude Code requires an entry "
                 f"for the repository itself, not for a parent directory")
        return (f"{where}; {key} is not trusted. Open `claude` once in {path} and "
                f"accept the trust dialog, then retry.")
    if accepted is not None and trusted(path, trust_file=trust_file):
        return None
    return (f"{path} is not trusted. Open `claude` once in {path} and accept the "
            f"trust dialog, then retry.")

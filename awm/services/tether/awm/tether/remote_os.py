"""Which kind of computer is on the owner's end, and what that means for an agent.

The operator declares it at ``invite`` because the owner's two launchers are
different programs in different shells, and an agent that guesses hands a
Windows user a line PowerShell cannot run. The declaration then travels with
the session so every later command is written for the right shell. The owner's
client also names its platform in its ``Hello``; that report is the truth about
the machine, and a disagreement with the declaration is surfaced, never
silently resolved.
"""

from __future__ import annotations

import re
from typing import Any

#: What the operator may declare: one value per launcher. WSL, Linux and macOS
#: all run the sh launcher, so they are one choice here and the calling agent
#: does the translating.
CHOICES: dict[str, dict[str, str]] = {
    "windows": {
        "launcher": "powershell",
        "shell": "PowerShell",
        "open": "Open PowerShell (Start menu, type PowerShell), paste this "
                "line, press Enter",
    },
    "unix": {
        "launcher": "sh",
        "shell": "POSIX sh",
        "open": "Open a terminal (on WSL, the WSL terminal such as Ubuntu, "
                "not PowerShell; on a Mac, Terminal), paste this line, press "
                "Enter",
    },
}

ASK = ("Ask the owner explicitly: Windows PowerShell, WSL, Linux or Mac? Offer "
       "WSL as its own option, because a WSL user may otherwise answer "
       "Windows. Pass `os: \"windows\"` for Windows PowerShell and "
       "`os: \"unix\"` for WSL, Linux or Mac. Do not guess: the launcher for "
       "one does not run on the other.")

#: Answers an owner might give that all mean the sh launcher.
UNIX_NAMES = frozenset({"wsl", "linux", "macos", "mac", "osx", "darwin",
                        "posix", "sh", "bash", "zsh"})

#: How the operator daemon's own line reads. The Windows line is built from the
#: same launcher URL and code, so the two can never name different sessions.
_SH_LINE = re.compile(r"^curl -fsSL (?P<url>\S+) \| sh -s (?P<code>.+)$")


def refusal(given: Any) -> str | None:
    """Why ``given`` is not a usable declaration, or None when it is."""
    if given is None or given == "":
        return f"invite needs `os`. {ASK}"
    if given in CHOICES:
        return None
    if str(given).strip().lower() in UNIX_NAMES:
        return (f"`os` is `windows` or `unix`, not `{given}`. WSL, Linux and "
                f"macOS all count as `unix`: send `os: \"unix\"`.")
    return f"`os` is `windows` or `unix`, not `{given}`. {ASK}"


def launcher_line(os_name: str, sh_line: str) -> str:
    """The one line the owner runs, for the shell they will run it in."""
    if CHOICES[os_name]["launcher"] == "sh":
        return sh_line
    match = _SH_LINE.match(sh_line)
    if match is None:
        raise ValueError(f"cannot read the launcher URL from `{sh_line}`")
    return (f"& ([scriptblock]::Create((irm {match['url']}/win))) "
            f"{match['code']}")


def _agrees(declared: str, reported: str) -> bool:
    # The client reports Rust's std::env::consts::OS. WSL runs the Linux
    # client, so every report but `windows` is a unix machine.
    return (reported == "windows") == (declared == "windows")


def describe(declared: str | None, reported: str | None = None) -> dict[str, Any]:
    """What an agent needs to know about a session's machine.

    ``reported`` is the platform the owner's client named on connect, such as
    ``windows``, ``linux`` or ``macos``; it is absent until the owner says yes.
    """
    out: dict[str, Any] = {"declared": declared}
    if reported:
        out["reported"] = reported
    choice = CHOICES.get(declared or "")
    if choice is None:
        out["warning"] = ("No OS was declared for this session. "
                          + (f"The owner's client reports `{reported}`; "
                             "confirm with the owner before running anything."
                             if reported else ASK))
        return out
    out["shell"] = choice["shell"]
    if reported and not _agrees(declared, reported):
        out["mismatch"] = True
        out["warning"] = (
            f"Declared `{declared}` but the owner's client reports "
            f"`{reported}`. The client is right about the machine it runs on: "
            f"write commands for `{reported}`, and ask the owner which "
            f"computer this is before going on.")
    return out

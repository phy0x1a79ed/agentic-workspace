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

#: What the operator may declare. The value is kept exactly as given: WSL and
#: Linux take the same launcher and report the same platform, but an agent
#: working in a WSL terminal needs to know Windows is underneath it.
CHOICES: dict[str, dict[str, str]] = {
    "windows": {
        "label": "Windows",
        "launcher": "powershell",
        "reports": "windows",
        "shell": "PowerShell",
        "open": "Open PowerShell (Start menu, type PowerShell), paste this "
                "line, press Enter",
        "notes": "Commands run in PowerShell (pwsh, else Windows PowerShell; "
                 "cmd.exe only when neither exists). Write PowerShell, not "
                 "sh: $env:NAME, Get-ChildItem, Test-Path, Select-String, "
                 "`;` between commands. Paths look like C:\\Users\\<name>. "
                 "grep, sed, uname and other POSIX tools are not there.",
    },
    "wsl": {
        "label": "WSL (Linux inside Windows)",
        "launcher": "sh",
        "reports": "linux",
        "shell": "POSIX sh (the owner's login shell, usually bash)",
        "open": "Open your WSL terminal (such as Ubuntu), not PowerShell, "
                "paste this line, press Enter",
        "notes": "A Linux distribution running inside Windows, with GNU "
                 "tools. Commands run in Linux, not on Windows itself. The "
                 "Windows drives are under /mnt/c, /mnt/d, and the Windows "
                 "user's files under /mnt/c/Users/<name>. Windows programs "
                 "run by their .exe name, such as powershell.exe or "
                 "explorer.exe. systemd and a desktop may be absent.",
    },
    "linux": {
        "label": "Linux",
        "launcher": "sh",
        "reports": "linux",
        "shell": "POSIX sh (the owner's login shell, usually bash)",
        "open": "Open a terminal, paste this line, press Enter",
        "notes": "GNU userland. The package manager depends on the "
                 "distribution (apt, dnf, pacman): read /etc/os-release "
                 "before installing anything.",
    },
    "macos": {
        "label": "macOS",
        "launcher": "sh",
        "reports": "macos",
        "shell": "POSIX sh (the owner's login shell, zsh by default)",
        "open": "Open Terminal (Applications > Utilities), paste this line, "
                "press Enter",
        "notes": "BSD userland, not GNU: `sed -i ''` needs its argument, and "
                 "date, stat, readlink, find and grep lack many GNU options. "
                 "The login shell is zsh. Packages come from Homebrew (brew) "
                 "when it is installed. Home is /Users/<name>.",
    },
}

ASK = ("Ask the owner which kind of computer they are on — Windows, WSL "
       "(Linux inside Windows), Linux or macOS — and pass it as `os`. Do not "
       "guess: the launcher for one does not run on the other.")

#: How the operator daemon's own line reads. The Windows line is built from the
#: same launcher URL and code, so the two can never name different sessions.
_SH_LINE = re.compile(r"^curl -fsSL (?P<url>\S+) \| sh -s (?P<code>.+)$")


def refusal(given: Any) -> str | None:
    """Why ``given`` is not a usable declaration, or None when it is."""
    if given is None or given == "":
        return f"invite needs `os`. {ASK}"
    if given not in CHOICES:
        return (f"`os` must be one of {', '.join(CHOICES)}, not `{given}`. "
                f"{ASK}")
    return None


def launcher_line(os_name: str, sh_line: str) -> str:
    """The one line the owner runs, for the shell they will run it in."""
    if CHOICES[os_name]["launcher"] == "sh":
        return sh_line
    match = _SH_LINE.match(sh_line)
    if match is None:
        raise ValueError(f"cannot read the launcher URL from `{sh_line}`")
    return (f"& ([scriptblock]::Create((irm {match['url']}/win))) "
            f"{match['code']}")


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
    out |= {k: choice[k] for k in ("label", "shell", "notes")}
    if reported and reported != choice["reports"]:
        out["mismatch"] = True
        out["warning"] = (
            f"Declared `{declared}` but the owner's client reports "
            f"`{reported}`. The client is right about the machine it runs on: "
            f"write commands for `{reported}`, and ask the owner which "
            f"computer this is before going on.")
    return out

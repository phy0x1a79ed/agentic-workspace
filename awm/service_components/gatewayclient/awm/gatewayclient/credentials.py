"""A singleton's secrets, from the credential systemd loads for its unit alone.

The gateway hands a spawned service the secrets its manifest's `needs.secrets`
names. A singleton runs under its own user and the gateway never spawns it, so
its unit loads them instead: ``LoadCredential=secrets.env:/etc/awm/secrets/<tool>.env``.
The source file is root's alone. systemd copies it into a directory only the
unit's user can read and names that directory in ``CREDENTIALS_DIRECTORY``.

The lines are the gateway's ``secrets.env`` lines, read the way
``supervisor::granted_secrets`` reads them. The two readers must agree.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import MutableMapping

CREDENTIAL = "secrets.env"


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ")
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        out[name.strip()] = value.strip().strip('"')
    return out


def load(environ: MutableMapping[str, str] = os.environ) -> list[str]:
    """Put the unit's secrets into `environ`, and name them.

    A name the unit's environment already sets keeps its value. Without a
    credential this does nothing: a service the gateway spawns has its secrets
    in its environment already.
    """
    directory = environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        return []
    try:
        text = (Path(directory) / CREDENTIAL).read_text("utf-8")
    except FileNotFoundError:
        return []
    names = []
    for name, value in parse(text).items():
        environ.setdefault(name, value)
        names.append(name)
    return names

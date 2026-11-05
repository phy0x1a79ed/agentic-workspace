"""Built files a release pins, and their push to the nodes that cannot fetch.

A pin is a `key=value` file in the project's tree that names one file in the
landing node's artifact store (`<AWM_DIR>/artifacts`) and its sha256, such as
the Trilium server tarball (install.md R115 item 4). `deploy.toml`'s
`[artifacts] pins` lists the pins. The landing node pushes each pinned file to
every push node that names an `artifacts` directory, before that node
refreshes. The node never fetches it (Tony, 2026-11-05: "mizar pushes rather
than deneb updates"). The install on the node checks the sha256 again.
"""

from __future__ import annotations

import asyncio
import hashlib
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from awm.routine import deployconf, proc


@dataclass(frozen=True)
class Artifact:
    pin: str
    file: str
    sha256: str


def parse_pin(text: str, pin: str) -> Artifact:
    fields = dict(line.split("=", 1) for line in text.splitlines()
                  if "=" in line and not line.lstrip().startswith("#"))
    name, sha = fields.get("file", "").strip(), fields.get("sha256", "").strip()
    if not name or not sha:
        raise ValueError(f"{pin} needs file and sha256")
    if "/" in name or name.startswith("."):
        raise ValueError(f"{pin}: file must be a bare file name, not {name!r}")
    return Artifact(pin, name, sha)


def pinned(bare: Path, rev: str) -> list[Artifact]:
    """The artifacts ``rev`` pins, read from the commit rather than a checkout."""
    try:
        conf = proc.git(bare, "show", f"{rev}:{deployconf.FILE}")
    except subprocess.CalledProcessError:
        return []
    out = []
    for pin in deployconf.parse(conf).artifact_pins:
        try:
            text = proc.git(bare, "show", f"{rev}:{pin}")
        except subprocess.CalledProcessError as e:
            raise ValueError(f"{deployconf.FILE} lists {pin}, which {rev[:12]} does not hold") from e
        out.append(parse_pin(text, pin))
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


async def check(store: Path, art: Artifact) -> None:
    """Refuse a pinned file that is missing here or whose bytes differ from the pin."""
    path = store / art.file
    if not path.is_file():
        raise ValueError(f"{art.pin} pins {art.file}, which {store} does not hold")
    got = await asyncio.to_thread(_sha256, path)
    if got != art.sha256:
        raise ValueError(f"{store / art.file} has sha256 {got}, and {art.pin} names {art.sha256}")


async def push(store: Path, art: Artifact, dest: str) -> proc.Result:
    """Copy one pinned file into ``dest`` (`<host>:<dir>`). rsync writes a temp
    file and renames it, so the node never sees half a file."""
    src = shlex.quote(str(store / art.file))
    to = shlex.quote(f"{dest.rstrip('/')}/{art.file}")
    return await proc.run(f"rsync -t --mkpath {src} {to}", cwd=str(store), timeout=900.0)

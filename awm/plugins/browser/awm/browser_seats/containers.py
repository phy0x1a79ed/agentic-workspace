"""A seat's own container, only when the seat asks for one (fleet.md R102 items 5-6).

The container's profile is a copy of Tony's or a blank one. Its debug port is
published on loopback only, for the singleton.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
from pathlib import Path

from awm.browser_seats.cdp import Chrome


async def _docker(docker: str, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(docker, *args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"{docker} {args[0]}: {err.decode(errors='replace').strip()}")
    return out.decode().strip()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Containers:
    def __init__(self, *, image: str, seats_dir: Path, tony_profile: Path | None, docker: str = "docker") -> None:
        self.image = image
        self.seats_dir = seats_dir
        self.tony_profile = tony_profile
        self.docker = docker

    async def launch(self, seat_id: str, profile: str) -> tuple[str, Chrome]:
        target = self.seats_dir / seat_id / "profile"
        if target.exists():
            shutil.rmtree(target)
        if profile == "tony":
            if self.tony_profile is None or not self.tony_profile.is_dir():
                raise RuntimeError("no copy of Tony's profile is configured on this browser")
            shutil.copytree(self.tony_profile, target, symlinks=True,
                            ignore=shutil.ignore_patterns("Singleton*", "*.lock"))
        else:
            target.mkdir(parents=True)
        port = _free_port()
        name = f"awm-browser-seat-{seat_id[:12].lower().replace('_', '-')}"
        await _docker(self.docker, "run", "-d", "--rm", "--name", name, "--shm-size", "1g",
                      "-p", f"127.0.0.1:{port}:{port}", "-e", f"CDP_PORT={port}",
                      "-v", f"{target}:/profile", self.image)
        for _ in range(60):
            try:
                return name, await Chrome(f"http://127.0.0.1:{port}").connect()
            except Exception:
                await asyncio.sleep(0.5)
        await self.stop(name)
        raise RuntimeError(f"seat container {name} did not open its debug port")

    async def stop(self, name: str) -> None:
        try:
            await _docker(self.docker, "rm", "-f", name)
        except RuntimeError:
            pass

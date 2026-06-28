"""Docker :class:`LaunchBackend` — runs the rlm-browser Chrome image in a
container (T3). Selected by ``RLM_BROWSER_BACKEND=docker``.

The contract is identical to the host backend: launch a Chrome, hand back a CDP
base URL on ``127.0.0.1:<port>``, stop it on release. The session driver and the
``cdp``/``commands`` relay never know the difference — they only ever speak CDP
to loopback. The single image branches simple vs rendered on ``RLM_MODE`` (the
``graphics`` flag), so this backend just maps mode → env + port publishing.

Port discipline: the container's internal CDP port is pinned to the SAME number
as the published host port (``-p 127.0.0.1:<port>:<port>``), so the
``webSocketDebuggerUrl`` Chrome advertises (which carries that port) is dialable
from the host unchanged. Rendered sessions additionally publish a noVNC port for
a human to watch at ``http://127.0.0.1:<vnc_port>/vnc.html``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from pathlib import Path

from awm.rlm_browser.browser import LaunchHandle, _await_cdp_ready, _free_port

log = logging.getLogger("awm.rlm_browser.docker_backend")

_IMAGE = os.environ.get("RLM_BROWSER_IMAGE", "rlm-browser:latest")
_SHM = os.environ.get("RLM_BROWSER_SHM", "2g")


class _DockerHandle(LaunchHandle):
    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            await _docker("rm", "-f", self.container_name)


async def _docker(*args: str) -> str:
    """Run ``docker <args>`` and return stdout; raise on non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        "docker", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker {' '.join(args)} failed ({proc.returncode}): "
            f"{err.decode(errors='replace').strip()}")
    return out.decode(errors="replace").strip()


class DockerBackend:
    name = "docker"

    async def launch(self, *, mode: str, profile_dir: str,
                     port: int = 0) -> LaunchHandle:
        port = port or _free_port()
        Path(profile_dir).mkdir(parents=True, exist_ok=True)
        container = f"rlm-browser-{uuid.uuid4().hex[:12]}"
        vnc_port = _free_port() if mode == "rendered" else 0

        # 1:1 port publish on loopback so the advertised ws URL is dialable.
        argv = [
            "run", "-d", "--name", container,
            f"--shm-size={_SHM}",
            "-p", f"127.0.0.1:{port}:{port}",
            "-v", f"{profile_dir}:/data",
            "-e", f"RLM_MODE={mode}",
            "-e", f"RLM_CDP_PORT={port}",
        ]
        if vnc_port:
            argv += ["-p", f"127.0.0.1:{vnc_port}:{vnc_port}",
                     "-e", f"RLM_NOVNC_PORT={vnc_port}"]
        argv += [_IMAGE]

        log.info("docker run %s (mode=%s, cdp=%d, vnc=%s)",
                 container, mode, port, vnc_port or "-")
        await _docker(*argv)
        handle = _DockerHandle(
            port=port, profile_dir=profile_dir, mode=mode,
            container_name=container, vnc_port=vnc_port)
        try:
            await _await_cdp_ready(handle.cdp_base, timeout=40.0)
        except Exception:
            # Surface the container logs on a boot failure, then clean up.
            with contextlib.suppress(Exception):
                logs = await _docker("logs", "--tail", "30", container)
                log.error("container %s failed to become ready:\n%s",
                          container, logs)
            await handle.stop()
            raise
        return handle

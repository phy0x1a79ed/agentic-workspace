"""Rendering a diagram to PDF/PNG/SVG through the containerized export server.

Two things this has to get right.

**Self-containment.** Cells reference images as ``/files/<abs-path>`` through
fileviewer's mount, which is origin-relative and therefore meaningless to
headless Chromium inside a container: it has no gateway, and the gateway binds
loopback anyway, so pointing the container at the host would mean opening a
listener purely so an exporter could read files that are already on the same
machine. Instead the service **inlines the bytes itself** before handing the
document over. The exporter then needs no network at all, and the output is
self-contained by construction rather than by hope — which is the property the
original prototype was built to verify.

**The semicolon landmine.** drawio splits style strings on ``;``, so the
conventional ``data:image/svg+xml;base64,…`` URI truncates at the first
semicolon and the cell renders blank. The fix, established by the prototype, is
the comma form with percent-encoded content: ``data:image/svg+xml,%3Csvg…``.
Encoding uses an empty safe-set so that a ``;`` *inside* the payload — routine
in SVG, which embeds CSS — is escaped too. Re-introducing a ``;base64,`` URI
anywhere in this path brings the landmine back.

Inlining is confined to the export path. The stored diagram keeps its
filesystem references, so re-rendering a molecule updates the diagram on reload
instead of requiring a re-import.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import httpx

log = logging.getLogger("awm.drawio.export")

#: The jgraph export server, run as a container the service owns.
CONTAINER = os.environ.get("DRAWIO_EXPORT_CONTAINER", "drawio-export")
IMAGE = os.environ.get("DRAWIO_EXPORT_IMAGE", "jgraph/export-server")
EXPORT_URL = os.environ.get("DRAWIO_EXPORT_URL", "http://127.0.0.1:8000")

FORMATS = ("pdf", "png", "jpg", "svg")

#: Same shape as the checker's pattern: ``/files`` followed directly by an
#: absolute path.
_FILES_REF = re.compile(r"/files(/[^\s;\"')<>]+)")

#: Refuse to inline anything absurd — a runaway reference would otherwise turn
#: one export into a gigabyte of percent-encoded text.
MAX_INLINE_BYTES = 24 * 1024 * 1024


class ExportError(RuntimeError):
    """The diagram could not be rendered."""


def data_uri(path: Path) -> str:
    """A ``data:`` URI safe to embed in a drawio style string.

    Deliberately *not* base64: the ``;base64`` marker contains the character
    drawio's style parser splits on. Percent-encoding with an empty safe-set is
    larger on the wire but survives the parser intact, which is the only
    property that matters here.
    """
    raw = path.read_bytes()
    if len(raw) > MAX_INLINE_BYTES:
        raise ExportError(
            f"{path} is {len(raw) / 1e6:.1f} MB, over the {MAX_INLINE_BYTES / 1e6:.0f} MB "
            "inline limit; shrink it or reference it from a lighter source"
        )
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if mime.startswith("text/") or mime in ("image/svg+xml",):
        payload = quote(raw.decode("utf-8", errors="replace"), safe="")
    else:
        payload = quote(raw, safe="")
    return f"data:{mime},{payload}"


def inline_images(xml: str) -> tuple[str, list[str]]:
    """Replace every ``/files/…`` reference with the file's bytes.

    Returns the rewritten document and the list of paths that could not be
    inlined. Missing references are left as-is rather than raising, so the
    caller decides whether a blank cell is acceptable — but they are reported,
    because a silently blank cell in an exported figure is exactly the failure
    this whole reference scheme risks.
    """
    problems: list[str] = []

    def replace(match: re.Match) -> str:
        target = Path(match.group(1))
        try:
            return data_uri(target)
        except (OSError, ExportError) as exc:
            problems.append(f"{target}: {exc}")
            return match.group(0)

    return _FILES_REF.sub(replace, xml), problems


# --- the container ---------------------------------------------------------

def _docker() -> str:
    binary = shutil.which("docker")
    if not binary:
        raise ExportError("docker is not available; export needs the "
                          f"{IMAGE} container")
    return binary


def container_state() -> str:
    """``running`` / ``stopped`` / ``absent`` / ``no-docker``."""
    try:
        binary = _docker()
    except ExportError:
        return "no-docker"
    proc = subprocess.run(
        [binary, "inspect", "-f", "{{.State.Status}}", CONTAINER],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return "absent"
    return "running" if proc.stdout.strip() == "running" else "stopped"


def ensure_container() -> str:
    """Start (or create) the export container. Idempotent."""
    binary = _docker()
    state = container_state()
    if state == "running":
        return state
    if state == "stopped":
        subprocess.run([binary, "start", CONTAINER], capture_output=True, check=False)
    elif state == "absent":
        # Bound to loopback: nothing outside this host has any business
        # reaching the exporter, and inlining means it never needs to reach out.
        subprocess.run(
            [binary, "run", "-d", "--name", CONTAINER,
             "-p", "127.0.0.1:8000:8000", IMAGE],
            capture_output=True, check=False,
        )
    else:
        raise ExportError("docker is not available")
    return container_state()


def render(xml: str, fmt: str = "pdf", *, inline: bool = True,
           scale: float = 1.0, page: int | None = None) -> tuple[bytes, list[str]]:
    """Render a diagram. Returns ``(bytes, problems)``.

    ``problems`` lists image references that could not be inlined — the caller
    should refuse to publish an export that has any.
    """
    fmt = (fmt or "pdf").lower()
    if fmt not in FORMATS:
        raise ExportError(f"unknown format {fmt!r} (known: {', '.join(FORMATS)})")

    problems: list[str] = []
    if inline:
        xml, problems = inline_images(xml)

    state = ensure_container()
    if state != "running":
        raise ExportError(
            f"export container {CONTAINER!r} is {state}; start it with "
            f"`docker run -d --name {CONTAINER} -p 127.0.0.1:8000:8000 {IMAGE}`"
        )

    payload: dict = {"xml": xml, "format": fmt, "scale": scale}
    if page is not None:
        payload["from"] = payload["to"] = int(page)
    if fmt == "svg":
        # Belt and braces: we have already inlined, but if a reference slipped
        # through, this keeps the SVG from silently depending on the network.
        payload["embedImages"] = 1

    try:
        response = httpx.post(f"{EXPORT_URL}/", data=payload, timeout=180)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ExportError(f"export server rejected the diagram: {exc}") from exc
    return response.content, problems

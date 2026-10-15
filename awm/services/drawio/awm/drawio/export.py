"""Rendering a diagram to PDF/PNG/SVG.

**Two backends, not one.** PDF, PNG and JPG go to the containerized export
server. SVG cannot: that container answers ``400 Unsupported Format!``, because
drawio has always produced SVG client-side from the live graph rather than on a
server. So SVG is routed to :mod:`awm.drawio.chrome`, which loads drawio's own
export client in headless Chrome and asks the rendered graph to serialize
itself. Everything below — inlining especially — applies to both.

Two things this has to get right.

**Self-containment.** Cells reference images two ways, and both are
origin-relative and therefore meaningless off this host: ``/files/<abs-path>``
through fileviewer's mount, and ``/drawio-app/view/…`` for a page of another
diagram rendered live. Headless Chromium inside the export container has no
gateway, and the gateway binds loopback anyway, so pointing the container at the
host would mean opening a listener purely so an exporter could read files that
are already on the same machine. Instead the service **inlines the bytes
itself** before handing the document over — reading the file for a ``/files``
reference, and *rendering the page* for a view reference. The exporter then
needs no network at all, and the output is self-contained by construction rather
than by hope — which is the property the original prototype was built to verify.

Both reference kinds are matched by **one** alternation rather than two passes.
That is not tidiness: a diagram stored under a path containing ``files``
produces a view URL with ``/files/…`` inside it, and a second pass would match
that fragment and rewrite the middle of a URL into a data URI for a file that
does not exist. One alternation removes the ordering question entirely.

A view reference can itself embed another, so resolving one is bounded three
ways — nesting depth, total nested renders, and total inlined bytes. Nesting
multiplies percent-encoding (a nested render is already full of encoded data
URIs, and re-encoding roughly doubles it per level), so without a byte budget an
over-composed figure fails as an opaque out-of-memory rather than as a reported
problem.

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

from . import recolour, renderspec

log = logging.getLogger("awm.drawio.export")

#: The jgraph export server, run as a container the service owns.
CONTAINER = os.environ.get("DRAWIO_EXPORT_CONTAINER", "drawio-export")
IMAGE = os.environ.get("DRAWIO_EXPORT_IMAGE", "jgraph/export-server")
EXPORT_URL = os.environ.get("DRAWIO_EXPORT_URL", "http://127.0.0.1:8000")

FORMATS = ("pdf", "png", "jpg", "svg")

#: Every image reference this service knows how to make self-contained, in one
#: alternation — see the module docstring for why it must be one and not two.
#:
#: The ``/files`` arm ends at the first character that could terminate it: ``;``
#: because that separates drawio style segments, and ``"`` / ``'`` / ``)``
#: because the same reference appears inside attribute values and CSS ``url()``.
#:
#: The view arm is deliberately more permissive about ``;``. Inside stored XML a
#: style attribute is escaped, so a multi-parameter query is spelled
#: ``…?swap=a:b&amp;swap=c:d`` — a terminator set containing ``;`` would cut the
#: URL at ``&amp`` and silently drop every parameter but the first. With
#: repeated ``swap`` that is the common case, not an edge, so the arm admits an
#: XML ampersand entity and nothing else that contains a semicolon — with the
#: entity branch **first**, because alternation is ordered and the ``*`` has
#: nothing after it that could force a backtrack: character-class-first, the
#: scan eats ``&``, ``a``, ``m``, ``p``, stops dead at the ``;``, and reports the
#: truncated URL as a successful match.
_VIEW_TAIL = r"(?:&(?:amp|#38|#x26);|[^\s;\"'<>])*"
REFERENCE_PATTERN = re.compile(
    r"(?P<view>(?:https?://[^/\s;\"'<>]+)?"
    + re.escape(renderspec.VIEW_PREFIX) + r"/" + _VIEW_TAIL + r")"
    r"|/files(?P<file>/[^\s;\"')<>]+)"
)

#: Refuse to inline anything absurd — a runaway reference would otherwise turn
#: one export into a gigabyte of percent-encoded text.
MAX_INLINE_BYTES = 24 * 1024 * 1024

#: The same ceiling, across one whole inlining pass rather than per reference.
#: Nesting is what makes this necessary; see the module docstring.
MAX_TOTAL_INLINE_BYTES = 96 * 1024 * 1024

#: How deep a view reference may embed another. Depth alone bounds the chain,
#: not the tree, which is what the render budget is for.
MAX_VIEW_DEPTH = 3

#: How many nested page renders one export may cost. A modest fan-out three
#: levels deep is hundreds of cold renders on a request-handler thread.
MAX_VIEW_RENDERS = 16

_AMP_ENTITY = re.compile(r"&(?:amp|#38|#x26);", re.IGNORECASE)


class ExportError(RuntimeError):
    """The diagram could not be rendered."""


class Budget:
    """Shared across one inlining pass, including everything it nests into.

    Carried on the resolver rather than threaded through every signature, so a
    nested render cannot start with a fresh allowance.
    """

    def __init__(self, max_bytes: int = MAX_TOTAL_INLINE_BYTES,
                 max_renders: int = MAX_VIEW_RENDERS):
        self.max_bytes = max_bytes
        self.max_renders = max_renders
        self.spent_bytes = 0
        self.renders = 0

    def spend_bytes(self, count: int) -> None:
        self.spent_bytes += count
        if self.spent_bytes > self.max_bytes:
            raise ExportError(
                f"inlining passed {self.spent_bytes / 1e6:.0f} MB, over the "
                f"{self.max_bytes / 1e6:.0f} MB budget for one export; the "
                "document embeds too much, or nests page views too deeply")

    def spend_render(self) -> None:
        self.renders += 1
        if self.renders > self.max_renders:
            raise ExportError(
                f"more than {self.max_renders} embedded page views in one "
                "export; flatten some of them")


# The factory a live service registers so that *any* caller of `render` — the
# export verb, an autopublish link, a nested view — resolves embedded page
# views the same way, against the same cache. Left unset, `inline_images` falls
# back to building one, so forgetting to register degrades to slower rather than
# to today's silent blindness.
_RESOLVER_FACTORY = None


def set_view_resolver_factory(factory) -> None:
    """Register how to resolve ``/drawio-app/view/…`` references."""
    global _RESOLVER_FACTORY
    _RESOLVER_FACTORY = factory


def default_view_resolver():
    if _RESOLVER_FACTORY is not None:
        return _RESOLVER_FACTORY()
    from .view import default_resolver

    return default_resolver()


def data_uri(path: Path, *, swaps: tuple[tuple[str, str], ...] = (),
             budget: "Budget | None" = None,
             problems: list[str] | None = None) -> str:
    """A ``data:`` URI safe to embed in a drawio style string.

    Deliberately *not* base64: the ``;base64`` marker contains the character
    drawio's style parser splits on. Percent-encoding with an empty safe-set is
    larger on the wire but survives the parser intact, which is the only
    property that matters here.

    Colour swaps are applied **here**, to the decoded bytes, before encoding.
    After encoding a ``#ff00ff`` in an SVG has become ``%23ff00ff``, so a later
    pass would have to match encoded spellings and would risk splicing a ``;``
    back into a style string. :mod:`awm.drawio.recolour` decides what a swap can
    reach — vector source, pixels, or nothing — and a swap it could not carry
    out is appended to ``problems`` rather than dropped.
    """
    raw = path.read_bytes()
    if len(raw) > MAX_INLINE_BYTES:
        raise ExportError(
            f"{path} is {len(raw) / 1e6:.1f} MB, over the {MAX_INLINE_BYTES / 1e6:.0f} MB "
            "inline limit; shrink it or reference it from a lighter source"
        )
    if budget is not None:
        budget.spend_bytes(len(raw))
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if swaps:
        raw, _hits, reported = recolour.swap_bytes(raw, mime, swaps)
        if reported and problems is not None:
            problems.extend(f"{path}: {problem}" for problem in reported)
    return f"data:{mime}," + quote(raw, safe="")


def svg_data_uri(data: bytes, *, budget: "Budget | None" = None) -> str:
    """The same comma-form URI for SVG bytes already in memory."""
    if budget is not None:
        budget.spend_bytes(len(data))
    return "data:image/svg+xml," + quote(
        data.decode("utf-8", errors="replace"), safe="")


def unescape_amp(text: str) -> str:
    """Undo the XML escaping a URL picks up from living in an attribute.

    Targeted rather than a full ``html.unescape``: the only entity a query
    string acquires on its way into a style attribute is the ampersand, and
    decoding more than that would corrupt a literal ``&lt;`` in a page name.
    """
    return _AMP_ENTITY.sub("&", text)


def inline_images(xml: str, *, swaps: tuple[tuple[str, str], ...] = (),
                  resolver=None,
                  budget: "Budget | None" = None) -> tuple[str, list[str]]:
    """Replace every image reference with the bytes it stands for.

    ``/files/…`` becomes the file's contents; ``/drawio-app/view/…`` becomes the
    render of that page, honouring the reference's *own* query, so two
    placements of one page in different colours embed as two different images.
    A reference's swaps are its own — the enclosing document's swaps do not
    cascade into an embedded page, because that page's URL is where its colours
    are decided.

    Returns the rewritten document and the list of references that could not be
    resolved. Failures are left in place rather than raised, so the caller
    decides whether a blank cell is acceptable — but they are reported, because
    a silently blank cell in an exported figure is exactly the failure this
    whole reference scheme risks. Problems from an embedded page fold upward
    with its URL as a prefix; without that, a break one level down is invisible
    at the top.
    """
    problems: list[str] = []
    spend = budget if budget is not None else Budget()
    lazy_resolver = [resolver]

    def _resolver():
        if lazy_resolver[0] is None:
            lazy_resolver[0] = default_view_resolver()
        return lazy_resolver[0]

    def replace(match: re.Match) -> str:
        if match.group("file") is not None:
            target = Path(match.group("file"))
            try:
                return data_uri(target, swaps=swaps, budget=spend,
                                problems=problems)
            except (OSError, ExportError) as exc:
                problems.append(f"{target}: {exc}")
                return match.group(0)

        url = unescape_amp(match.group("view"))
        try:
            data, nested = _resolver()(url, spend)
        except Exception as exc:  # noqa: BLE001 — every failure is a report
            problems.append(f"{url}: {exc}")
            return match.group(0)
        problems.extend(f"{url} -> {problem}" for problem in nested)
        try:
            return svg_data_uri(data, budget=spend)
        except ExportError as exc:
            problems.append(f"{url}: {exc}")
            return match.group(0)

    return REFERENCE_PATTERN.sub(replace, xml), problems


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
           scale: float = 1.0, page: int | None = None,
           swaps: tuple[tuple[str, str], ...] = (), crop: str | None = None,
           crop_id: str | None = None, resolver=None,
           budget: "Budget | None" = None) -> tuple[bytes, list[str]]:
    """Render a diagram. Returns ``(bytes, problems)``.

    ``problems`` lists image references that could not be inlined — the caller
    should refuse to publish an export that has any.

    ``swaps`` and ``crop`` are the view URL's parameters, applied here so that a
    one-shot export, a standing autopublish link and a placed page view are the
    same mechanism rather than three that happen to agree. ``crop_id`` is the
    already-resolved form, for the view layer, which needs the prepared document
    to compute its content key before this is called.

    Swapping happens **before** inlining: the document still holds ``/files``
    references at that point, so the colour rewrite never has to reason about a
    percent-encoded payload.

    Callers must not hold the browser lock across this — inlining can render an
    embedded page, and ``chrome``'s lock is not re-entrant. Inlining strictly
    preceding the render call is what makes nesting safe.
    """
    fmt = (fmt or "pdf").lower()
    if fmt not in FORMATS:
        raise ExportError(f"unknown format {fmt!r} (known: {', '.join(FORMATS)})")

    # ValueError subclasses from the spec layer become ExportError here: every
    # caller of this function branches on ExportError, and a raw
    # CompressedDiagram would escape all of them.
    problems: list[str] = []
    try:
        if swaps:
            xml, _, problems = renderspec.swap_document(xml, swaps)
        if crop and not crop_id:
            xml, crop_id = renderspec.prepare_crop(xml, page, crop)
    except ValueError as exc:
        raise ExportError(str(exc)) from exc
    if crop_id and fmt != "svg":
        raise ExportError(
            f"crop= needs an exact render of one shape, which only the SVG path "
            f"can do; {fmt} goes through the export container")

    if inline:
        xml, inlined_problems = inline_images(xml, swaps=swaps,
                                              resolver=resolver, budget=budget)
        problems = problems + inlined_problems

    if fmt == "svg":
        # SVG does not go through the container at all — it cannot make one.
        # See awm.drawio.chrome for why this needs a browser.
        from . import chrome

        try:
            svg = chrome.render_svg(xml, scale=scale, page=page,
                                    crop_id=crop_id)
        except chrome.ChromeError as exc:
            raise ExportError(str(exc)) from exc
        return svg.encode("utf-8"), problems

    state = ensure_container()
    if state != "running":
        raise ExportError(
            f"export container {CONTAINER!r} is {state}; start it with "
            f"`docker run -d --name {CONTAINER} -p 127.0.0.1:8000:8000 {IMAGE}`"
        )

    payload: dict = {"xml": xml, "format": fmt, "scale": scale}
    if page is not None:
        payload["from"] = payload["to"] = int(page)

    try:
        response = httpx.post(f"{EXPORT_URL}/", data=payload, timeout=180)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ExportError(f"export server rejected the diagram: {exc}") from exc
    return response.content, problems

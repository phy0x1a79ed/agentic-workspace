"""Post-processing an exported Penpot board SVG: colour ``swap`` and ``crop``.

:mod:`awm.penpot_view.renderspec` owns the query grammar (``scale=``,
``swap=<from>:<to>``, ``crop=<name>``) and the cache identity; this module is
what actually rewrites the SVG bytes the exporter handed back. ``scale`` is
not this module's job at all -- it is a native parameter the exporter applies
while rendering, upstream of anything here. Only ``swap`` and ``crop`` are
post-processing, and they are the whole of this module.

**This is not a port of :mod:`awm.drawio.recolour`, and deliberately so.** A
``.drawio`` document is a container that can hold an imported raster or
another SVG as a base64/percent-encoded ``data:`` URI, so drawio's module
spends most of its bulk sniffing codecs, decoding Pillow images and shifting
pixels. An exported Penpot board is a single, self-contained SVG document
with no embedded foreign images to decode -- every colour in it is text, and
every colour that matters is spelled the same way. That collapses the whole
"three surfaces" problem drawio has to solve down to one: rewrite ``rgb()``
inside a ``style=`` attribute.

**What a real Penpot export actually looks like** (confirmed against a live
sample, not assumed): a shape is a ``<g id="shape-<uuid>">``. Fill and stroke
are **inline CSS declarations inside a ``style=`` attribute**, in functional
``rgb(r, g, b)`` notation --
``style="fill: rgb(255, 255, 255); fill-opacity: 1;"`` -- never a bare
``fill="#rrggbb"`` XML attribute. (A bare ``fill=``/``stroke=``/
``stop-color=`` attribute *does* appear in real output, but only ever holding
``none`` -- Penpot uses it to suppress a paint, not to spell a colour.) The
root ``<svg>`` also carries a *second*, unrelated id --
``id="screenshot-<uuid>"`` -- which is the exporter's own render-completion
locator (:mod:`awm.penpot_view` polls for that id to know the page finished
drawing) and has nothing to do with ``crop``; conflating the two would crop
against the wrong element or, worse, silently against nothing. A board's root
frame is wrapped in a ``<clipPath id="frame-clip-<uuid>-render-N">`` holding
one ``<rect>`` -- see *crop* below for why that rect matters far beyond
clipping.

**swap.** Because every colour lives inside one attribute in one notation,
plain regex over ``style="..."`` values is simpler and safer here than a full
XML parse would be: it touches only the bytes that can hold a colour and
leaves everything else -- attribute order, quoting, self-closing style --
byte-for-byte untouched, which is what makes "a swap that matches nothing
returns the input unchanged" a trivial guarantee rather than something a
serializer could quietly break. All ``rgb()`` occurrences are matched against
every requested source colour in **one pass**: a match is looked up in a
plain ``{source: target}`` dict built once, so a rewritten ``a -> b`` is
never itself re-scanned and turned into ``c`` by a later ``b -> c`` entry in
the same request. Source/target colours arrive from
:mod:`awm.penpot_view.renderspec` already canonicalised (lowercase 6-digit
hex) by its ``canonical_colour``/``parse_swaps``; this module trusts that and
never re-derives hex parsing, except to reuse ``canonical_colour`` itself
when checking a bare attribute below.

A swap that matches no ``rgb()`` anywhere is not an error -- the requested
colour may simply not be on this board. But a colour that *is* present, just
somewhere this module does not rewrite, is a different failure and is
reported rather than silently dropped: if a requested source colour turns up
as a bare ``fill=``/``stroke=``/``stop-color=`` XML attribute (not inside
``style=``), that occurrence is left alone and a problem string is appended,
following the sibling ``(data, problems)`` convention used by
:mod:`awm.drawio.export`.

**Unverified, on purpose, not papered over:** the one live sample available
while writing this was a plain white-filled frame with no gradient and no
text. A gradient stop written as ``style="stop-color: rgb(...)"`` would be
caught by the same regex, since it does not care which CSS property the
``rgb()`` sits inside -- but that is inference, not confirmation. Text
flattened to a path is, likewise, just another element with its own
``style="fill: rgb(...)"`` if Penpot spells it the same way everything else
does -- also unconfirmed. Neither case gets special-cased here; if either
turns out to need one, the ``problems`` channel above is exactly where a
future fix would report the gap rather than a caller silently getting an
unchanged picture.

**crop.** :mod:`renderspec` documents ``crop=<name>`` as naming a shape "by
name", but the exported SVG carries no human-readable label anywhere in it --
only ``id="shape-<uuid>"``. Resolving a Penpot layer name to that uuid would
mean asking the Penpot API for the board's shape tree, which needs the file
open elsewhere in this service and is out of reach of a function that only
gets SVG bytes. So this module resolves ``crop`` the only way SVG bytes
alone permit: the value handed in must already be the shape's id (the uuid,
with or without the ``shape-`` prefix). An unresolvable **name** is refused
with :class:`ShapeNotFound` rather than silently rendering the un-cropped
board.

Cropping rewrites the root ``<svg>``'s ``viewBox``/``width``/``height`` and
leaves the ``frame-clip`` wrapper alone, per the spike: fighting the wrapper
by deleting or resizing it risks corrupting geometry this module does not
otherwise touch, whereas a smaller ``viewBox`` alone already restricts what
is visible -- everything outside it is simply off-canvas, the same as
scrolling a window.

The bounding box used for that ``viewBox`` must be the *rendered* box --
after strokes and effects, not the Penpot editor's model box -- or a crop
clips content that is visibly still there. Two tiers, in order, and no
guessing beyond them:

1. If the target shape is itself a frame, it already carries its own
   ``frame-clip-<uuid>`` rect. Penpot computed that rect as the frame's own
   authoritative render boundary, so this module simply reads it (applying
   its own ``transform``) rather than re-deriving anything -- this is the
   one case confirmed against the live sample.
2. Otherwise -- a plain shape with no frame-clip of its own -- this module
   falls back to unioning the boxes of every ``<rect>`` in the shape's
   subtree (skipping ``<defs>``/``<clipPath>`` contents, which are geometry
   Penpot uses for clipping, not for painting), widened by half of a rect's
   own ``stroke-width`` when one is present. This is a best-effort
   approximation, **not verified against a live export**: it does not
   account for filter/shadow extents, and it does not walk transforms above
   the target shape's own subtree -- it trusts that each rect's own
   ``transform`` is already root-relative, which is what the one confirmed
   sample showed but is not proven for a deeply nested shape.

If neither tier finds any rect geometry at all -- a shape built only from
curved paths, text-as-path, or other primitives this module does not
understand -- cropping to it raises :class:`SvgPostError` naming exactly
that, rather than shipping a crop computed from thin air.
"""

from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET

from . import renderspec as R

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

_G = f"{{{SVG_NS}}}g"
_RECT = f"{{{SVG_NS}}}rect"
_CLIPPATH = f"{{{SVG_NS}}}clipPath"
_DEFS = f"{{{SVG_NS}}}defs"

# Keeps `crop_svg`'s ElementTree round trip spelling the default namespace
# `xmlns="..."` the way the exporter wrote it, instead of ET's fallback
# `ns0:` prefix -- a cosmetic difference that would otherwise make every
# untouched tag in a cropped render look like a different document.
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)

#: A double-quoted `style="..."` attribute value. Penpot's exporter is only
#: ever observed to double-quote attributes; a single-quoted style is
#: unverified and not matched.
_STYLE_ATTR = re.compile(r'style="([^"]*)"')

#: CSS functional colour notation, as it appears inside a style value.
_RGB = re.compile(r"rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)")

#: A colour spelled as a bare XML attribute rather than inside style= -- the
#: shape this module deliberately does not rewrite; see the module docstring.
_BARE_COLOUR_ATTR = re.compile(r'\b(fill|stroke|stop-color)="([^"]*)"')

#: An SVG `matrix(a, b, c, d, e, f)` transform, as Penpot writes it on a rect.
_MATRIX = re.compile(
    r"matrix\(\s*([-\d.eE]+)\s*,\s*([-\d.eE]+)\s*,\s*([-\d.eE]+)\s*,\s*"
    r"([-\d.eE]+)\s*,\s*([-\d.eE]+)\s*,\s*([-\d.eE]+)\s*\)"
)

_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

Matrix = tuple[float, float, float, float, float, float]
BBox = tuple[float, float, float, float]


#: An absolute ``http(s)`` reference the exporter left pointing back at
#: Penpot: `href="..."` on an `<image>`, or `url(...)` inside the `@font-face`
#: rules in the document's own `<style>` block. Both forms are matched in one
#: pass so a single rewrite pulls the whole document self-contained.
_EXTERNAL_REF = re.compile(
    r'(?P<attr>\b(?:xlink:href|href)=")(?P<hurl>https?://[^"]+)"'
    r'|(?P<func>url\(\s*)(?P<uurl>https?://[^)\s]+)\s*\)'
)


class SvgPostError(ValueError):
    """A ``swap`` or ``crop`` could not be honoured against this SVG."""


class ShapeNotFound(SvgPostError):
    """``crop`` named a shape id with no matching ``shape-<id>`` element."""


# --- swap --------------------------------------------------------------

def swap_svg(svg: bytes, swaps: tuple[tuple[str, str], ...]
             ) -> tuple[bytes, list[str]]:
    """Rewrite ``rgb()`` colours inside every ``style=`` attribute.

    Applies all of ``swaps`` in one pass, so ``a -> b`` and ``b -> c``
    requested together never chain into ``a -> c`` -- see the module
    docstring. Returns ``(bytes, problems)``; ``problems`` names a requested
    source colour that exists in the document only as a bare colour
    attribute this module does not rewrite. A source colour absent from the
    document entirely is not a problem, and the bytes come back
    byte-for-byte identical when no swap touches anything.
    """
    if not swaps:
        return svg, []

    text = svg.decode("utf-8")
    lookup = dict(swaps)

    def _rgb_sub(match: re.Match) -> str:
        r, g, b = (int(match.group(i)) for i in (1, 2, 3))
        hexval = f"{r:02x}{g:02x}{b:02x}"
        target = lookup.get(hexval)
        if target is None:
            return match.group(0)
        tr, tg, tb = int(target[0:2], 16), int(target[2:4], 16), int(target[4:6], 16)
        return f"rgb({tr}, {tg}, {tb})"

    def _style_sub(match: re.Match) -> str:
        return f'style="{_RGB.sub(_rgb_sub, match.group(1))}"'

    text = _STYLE_ATTR.sub(_style_sub, text)

    # A source colour is checked against bare attributes regardless of
    # whether it also matched inside style= elsewhere: those are two
    # different occurrences in the document, and a hit on one must not mask
    # a miss on the other -- a partially-applied swap is still a gap to
    # report.
    problems: list[str] = []
    for source, target in swaps:
        for attr_match in _BARE_COLOUR_ATTR.finditer(text):
            attr_name, value = attr_match.groups()
            try:
                hexval = R.canonical_colour(value)
            except R.SpecError:
                continue  # `fill="none"` and the like -- not a colour at all
            if hexval == source:
                problems.append(
                    f"swap {source}->{target}: found as a bare "
                    f'{attr_name}="{value}" attribute, not inside style=; '
                    "this module only rewrites colours inside style= so it "
                    "was left unchanged"
                )
                break

    return text.encode("utf-8"), problems


# --- crop ----------------------------------------------------------------

def _matrix(transform: str | None) -> Matrix:
    if not transform:
        return _IDENTITY
    match = _MATRIX.search(transform)
    if not match:
        return _IDENTITY
    a, b, c, d, e, f = (float(g) for g in match.groups())
    return (a, b, c, d, e, f)


def _apply(matrix: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return (a * x + c * y + e, b * x + d * y + f)


def _stroke_padding(style: str) -> float:
    """Half of a rect's own ``stroke-width``, or 0 if it has none/no stroke."""
    stroke = re.search(r"stroke:\s*([^;]+)", style)
    if not stroke or stroke.group(1).strip() in ("", "none"):
        return 0.0
    width = re.search(r"stroke-width:\s*([\d.]+)", style)
    return float(width.group(1)) / 2 if width else 0.0


def _rect_bbox(rect: ET.Element) -> BBox | None:
    try:
        x = float(rect.get("x", "0"))
        y = float(rect.get("y", "0"))
        w = float(rect.get("width", "0"))
        h = float(rect.get("height", "0"))
    except ValueError:
        return None
    if w <= 0 or h <= 0:
        return None
    matrix = _matrix(rect.get("transform"))
    corners = [
        _apply(matrix, x, y), _apply(matrix, x + w, y),
        _apply(matrix, x, y + h), _apply(matrix, x + w, y + h),
    ]
    xs = [px for px, _ in corners]
    ys = [py for _, py in corners]
    pad = _stroke_padding(rect.get("style") or "")
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


def _rects_under(elem: ET.Element):
    """``<rect>`` descendants of ``elem``, not descending into non-rendering
    ``<defs>``/``<clipPath>`` subtrees (which hold clip geometry, not paint)."""
    for child in elem:
        if child.tag in (_DEFS, _CLIPPATH):
            continue
        if child.tag == _RECT:
            yield child
        else:
            yield from _rects_under(child)


def _frame_clip_bbox(shape_id: str, elem: ET.Element) -> BBox | None:
    """The rect of ``elem``'s own ``frame-clip-<shape_id>[-...]``, if any.

    Confirmed shape: this is exactly the box Penpot itself computed as the
    frame's render boundary, so it is used verbatim rather than re-derived.
    """
    prefix = f"frame-clip-{shape_id}"
    for clip in elem.iter(_CLIPPATH):
        clip_id = clip.get("id") or ""
        if clip_id == prefix or clip_id.startswith(prefix + "-"):
            rect = clip.find(_RECT)
            if rect is not None:
                return _rect_bbox(rect)
    return None


def crop_svg(svg: bytes, name: str) -> bytes:
    """Rewrite the root ``<svg>``'s ``viewBox``/``width``/``height`` to the
    rendered bounding box of the shape ``name`` resolves to.

    ``name`` must be a shape id (the uuid, with or without the ``shape-``
    prefix) -- see the module docstring for why a human label cannot be
    resolved here. Raises :class:`ShapeNotFound` if no such shape is in the
    document, and :class:`SvgPostError` if a matching shape exists but no
    rendered geometry for it is recoverable from the SVG alone.
    """
    text = svg.decode("utf-8")
    root = ET.fromstring(text)

    shape_id = name[len("shape-"):] if name.startswith("shape-") else name
    target = None
    for g in root.iter(_G):
        if g.get("id") == f"shape-{shape_id}":
            target = g
            break
    if target is None:
        raise ShapeNotFound(
            f"no shape-{shape_id} element for crop={name!r} in this render")

    bbox = _frame_clip_bbox(shape_id, target)
    if bbox is None:
        boxes = [b for r in _rects_under(target) if (b := _rect_bbox(r)) is not None]
        if not boxes:
            raise SvgPostError(
                f"crop={name!r}: no rendered geometry recoverable for this "
                "shape from the SVG alone -- it has neither a frame-clip "
                "nor a <rect> descendant; curved paths, text-as-path and "
                "filter/shadow extents are not computed by this module")
        xs0 = [b[0] for b in boxes]
        ys0 = [b[1] for b in boxes]
        xs1 = [b[2] for b in boxes]
        ys1 = [b[3] for b in boxes]
        bbox = (min(xs0), min(ys0), max(xs1), max(ys1))

    min_x, min_y, max_x, max_y = bbox
    width, height = max_x - min_x, max_y - min_y
    if width <= 0 or height <= 0:
        raise SvgPostError(
            f"crop={name!r}: degenerate bounding box ({width:g}x{height:g})")

    root.set("viewBox", f"{min_x:g} {min_y:g} {width:g} {height:g}")
    root.set("width", f"{width:g}")
    root.set("height", f"{height:g}")
    return ET.tostring(root, encoding="unicode").encode("utf-8")


# --- scale -----------------------------------------------------------------

#: The root `<svg>`'s presentation width/height, as the exporter writes them:
#: a bare number of user units, no unit suffix.
_ROOT_DIM = re.compile(rb'(?P<attr>\b(width|height)=")(?P<value>[\d.]+)"')


def scale_svg(svg: bytes, factor: float) -> bytes:
    """Multiply the root ``<svg>``'s presentation width and height, leaving
    its ``viewBox`` alone.

    ``scale`` is declared as a native exporter parameter and *is* sent as one,
    but Penpot only applies it to the bitmap formats: an SVG export comes back
    at the board's own dimensions whatever scale was asked for (verified live
    -- ``scale=2`` returned a byte-for-byte identical root element). SVG has
    no resolution to scale in the first place, so the meaningful reading of
    ``scale`` here is the presentation size: the same vector content laid out
    at N times the size, which is exactly width/height against an unchanged
    viewBox. Doing it here rather than dropping the parameter keeps a
    ``scale=`` request honest instead of silently ignored.

    Only the *first* width/height pair is touched -- that is the root element,
    and every later one belongs to a shape whose geometry the viewBox already
    governs.
    """
    if factor == 1.0:
        return svg
    head, sep, tail = svg.partition(b">")
    if not sep:
        return svg
    seen = 0

    def rewrite(match: "re.Match[bytes]") -> bytes:
        nonlocal seen
        seen += 1
        if seen > 2:
            return match.group(0)
        value = float(match.group("value")) * factor
        return match.group("attr") + f"{value:g}".encode() + b'"'

    return _ROOT_DIM.sub(rewrite, head) + sep + tail


# --- inlining ---------------------------------------------------------------

def inline_externals(svg: bytes, fetch) -> tuple[bytes, list[str]]:
    """Replace every absolute ``http(s)`` reference with a ``data:`` URI, so
    the render is self-contained.

    ``fetch(url) -> (content_type, bytes)`` does the retrieval and owns the
    decision about *which* URLs may be fetched at all -- see
    :meth:`awm.penpot_view.exporter_client.ExporterClient.fetch_subresource`,
    which refuses anything not same-origin with Penpot itself.

    A reference that cannot be fetched is **left exactly as it was** and
    reported as a problem. That is the honest degradation: the render still
    carries the reference it was born with, and the caller sees in
    ``X-Penpot-Problems`` that part of the document did not come along --
    rather than a silently holed image, which is the failure this whole
    service exists to refuse. Each distinct URL is fetched once however many
    times the document references it.
    """
    cache: dict[str, bytes | None] = {}
    problems: list[str] = []

    def resolve(url: str) -> bytes | None:
        if url not in cache:
            try:
                content_type, data = fetch(url)
            except Exception as exc:  # noqa: BLE001 -- any failure degrades alike
                problems.append(f"could not inline {url}: {exc}")
                cache[url] = None
            else:
                encoded = base64.b64encode(data).decode("ascii")
                cache[url] = f"data:{content_type};base64,{encoded}".encode()
        return cache[url]

    def rewrite(match: "re.Match[str]") -> str:
        url = match.group("hurl") or match.group("uurl")
        inlined = resolve(url)
        if inlined is None:
            return match.group(0)
        value = inlined.decode("ascii")
        if match.group("hurl") is not None:
            return f'{match.group("attr")}{value}"'
        return f'{match.group("func")}{value})'

    text = svg.decode("utf-8", errors="surrogateescape")
    out = _EXTERNAL_REF.sub(rewrite, text)
    return out.encode("utf-8", errors="surrogateescape"), problems


# --- combined --------------------------------------------------------------

def postprocess(svg: bytes, spec: R.RenderSpec) -> tuple[bytes, list[str]]:
    """Apply ``spec.swap``, ``spec.crop`` and ``spec.scale`` to exported SVG
    bytes.

    ``scale`` runs last: it rewrites the root element's presentation size, and
    doing that before a crop would mean cropping against dimensions the
    viewBox no longer agrees with.
    """
    data, problems = swap_svg(svg, spec.swaps)
    if spec.crop:
        data = crop_svg(data, spec.crop)
    return scale_svg(data, spec.scale), problems

"""Post-processing an exported Penpot board SVG: colour ``swap`` and ``crop``.

:mod:`awm.penpot_view.renderspec` owns the query grammar (``scale=``,
``swap=<from>:<to>``, ``crop=<name>``) and the cache identity; this module is
what actually rewrites the SVG bytes the exporter handed back: ``swap``,
``crop``, and -- because Penpot ignores it for SVG (verified live) --
``scale``, applied here as the root element's presentation size.

**This is not a port of :mod:`awm.drawio.recolour`, and deliberately so.** A
``.drawio`` document is a container that can hold an imported raster or
another SVG as a base64/percent-encoded ``data:`` URI, so drawio's module
spends most of its bulk sniffing codecs, decoding Pillow images and shifting
pixels. An exported Penpot board is a single, self-contained SVG document
with no embedded foreign images to decode -- every colour in it is text, and
every colour that matters is spelled one of two ways. That collapses the
whole "three surfaces" problem drawio has to solve down to two regex passes.

**What a real Penpot export actually looks like** (confirmed against a live
sample, not assumed): a shape is a ``<g id="shape-<uuid>">``. Fill and stroke
are **inline CSS declarations inside a ``style=`` attribute**, in functional
``rgb(r, g, b)`` notation --
``style="fill: rgb(255, 255, 255); fill-opacity: 1;"``. A **gradient stop**
is the exception, and the one that matters: it arrives as a bare
``stop-color="#b1b2b5"`` XML attribute holding hex, never as ``rgb()`` inside
``style=`` -- confirmed against a live export of a board carrying two linear
gradients. A bare ``fill=``/``stroke=`` attribute also appears, holding
``none`` (Penpot suppressing a paint) or a ``url(#...)`` reference. The
root ``<svg>`` also carries a *second*, unrelated id --
``id="screenshot-<uuid>"`` -- which is the exporter's own render-completion
locator (:mod:`awm.penpot_view` polls for that id to know the page finished
drawing) and has nothing to do with ``crop``; conflating the two would crop
against the wrong element or, worse, silently against nothing. A board's root
frame is wrapped in a ``<clipPath id="frame-clip-<uuid>-render-N">`` holding
one ``<rect>`` -- see *crop* below for why that rect matters far beyond
clipping.

**swap.** Because every colour lives inside an attribute in one of two known
notations, plain regex is simpler and safer here than a full XML parse would
be: it touches only the bytes that can hold a colour and
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

A swap that matches nothing is not an error -- the requested colour may
simply not be on this board. What *is* reported, through the sibling
``(data, problems)`` convention :mod:`awm.drawio.export` uses, is a paint
spelled in a form this module cannot parse at all (a CSS colour name, say):
such a value is skipped by both passes, and skipping it silently would hand
a caller an unchanged picture alongside a clean report.

**The one colour still out of reach, and it is Penpot's own bug.** Text
carries its fill indirectly, as ``fill: url(#fill-0-render-N)`` pointing at a
generated def. Rewriting the def's own colour does reach it -- but in the
live export the reference is *dangling*: the document defines
``fill-0-render-9-0`` while the text points at ``fill-0-render-9``, so
exported text renders unfilled whatever this module does. That is an upstream
fidelity defect, not something a colour swap can repair.

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
    # An <image>'s href, and *only* an <image>'s. Matching a bare href= would
    # also rewrite an <a href> into a data:text/html link, which is neither
    # wanted nor safe.
    r'(?P<attr><image\b[^>]*?\b(?:xlink:)?href=")(?P<hurl>https?://[^"]+)"'
    # A CSS url(), quoted or not. Penpot's own @font-face rules are unquoted,
    # but a quoted one that silently stayed external would be exactly the
    # fallback-font failure this inlining exists to fix.
    r"""|(?P<func>url\(\s*)(?P<q>["']?)(?P<uurl>https?://[^)"'\s]+)(?P=q)\s*\)"""
)


#: `type/subtype` and nothing else. See `inline_externals`.
_MEDIA_TYPE = re.compile(r"^[\w.+-]+/[\w.+-]+$")


class SvgPostError(ValueError):
    """A ``swap`` or ``crop`` could not be honoured against this SVG."""


class ShapeNotFound(SvgPostError):
    """``crop`` named a shape id with no matching ``shape-<id>`` element."""


# --- swap --------------------------------------------------------------

def swap_svg(svg: bytes, swaps: tuple[tuple[str, str], ...]
             ) -> tuple[bytes, list[str]]:
    """Rewrite colours in the two forms Penpot's exporter actually emits.

    A shape fill arrives as ``rgb()`` inside a ``style=`` attribute; a
    gradient stop arrives as a bare ``stop-color="#hex"`` attribute. Both are
    rewritten, in two passes over disjoint text, so ``a -> b`` and ``b -> c``
    requested together still never chain into ``a -> c``.

    Returns ``(bytes, problems)``. ``problems`` names a source colour still
    present in a rewritable form afterwards -- a post-condition on the passes
    above rather than a prediction about the document. A source colour absent
    from the document entirely is not a problem, and the bytes come back
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

    # Gradient stops are the reason this second pass exists. Penpot writes a
    # shape fill as `style="fill: rgb(...)"` but a gradient's stops as bare
    # `stop-color="#b1b2b5"` attributes carrying a hex value -- verified live
    # against a board with two linear gradients. Rewriting only style= would
    # leave every gradient in the document at its original colour while
    # reporting the swap as applied everywhere else, which is precisely the
    # half-done render this module is supposed to refuse. The two passes
    # touch disjoint text, so applying both still cannot chain a -> b -> c.
    def _attr_sub(match: "re.Match[str]") -> str:
        attr_name, value = match.groups()
        try:
            hexval = R.canonical_colour(value)
        except R.SpecError:
            return match.group(0)  # `fill="none"` and the like
        target = lookup.get(hexval)
        if target is None:
            return match.group(0)
        return f'{attr_name}="#{target}"'

    text = _BARE_COLOUR_ATTR.sub(_attr_sub, text)

    # A problem means "you asked for a swap and part of the document did not
    # get it", so it has to be computed against `swaps` -- not against the
    # document alone, which both over-fires (any `currentColor` anywhere
    # reports a problem on a render whose swap applied perfectly) and
    # under-fires (a colour this module cannot see is not a colour it can
    # report by name).
    problems: list[str] = []
    for source, target in swaps:
        if _hex_in_style(text, source):
            problems.append(
                f"swap {source}->{target}: #{source} appears inside a style= "
                "declaration, which neither pass rewrites, so this render is "
                "only partly swapped")
    # Reported once for the document, not once per swap, and worded as a
    # caveat: an unparseable spelling *may* denote a colour that was asked
    # for, and claiming it definitely did would be its own false report.
    for attr_name, value in _unparseable_paints(text):
        problems.append(
            f'{attr_name}="{value}" is a colour spelling this module cannot '
            "parse; if it denotes one of the requested source colours, that "
            "occurrence was left unchanged")
    return text.encode("utf-8"), problems


def _hex_in_style(text: str, colour: str) -> bool:
    """Whether ``colour`` appears as a hex literal inside a ``style=`` value.

    Neither pass rewrites that form -- :data:`_RGB` reads only ``rgb()`` and
    :data:`_BARE_COLOUR_ATTR` needs a bare attribute -- so
    ``style="fill: #ff0000"`` comes back unchanged. Left unreported that is
    exactly the "unchanged picture alongside a clean report" this channel
    exists to prevent.
    """
    lowered = text.lower()
    return f"#{colour}" in "".join(
        m.group(1) for m in _STYLE_ATTR.finditer(lowered))


def _unparseable_paints(text: str) -> list[tuple[str, str]]:
    """Distinct bare paint attributes whose value is not a colour this module
    can parse -- a CSS colour name, say. ``none`` and ``url(...)`` are not
    colour spellings that failed, so neither is reported.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _BARE_COLOUR_ATTR.finditer(text):
        attr_name, value = match.groups()
        if value in ("none", "") or value.startswith("url(") or value in seen:
            continue
        try:
            R.canonical_colour(value)
        except R.SpecError:
            seen.add(value)
            out.append((attr_name, value))
    return out


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

#: The root `<svg>` element, skipping any XML declaration, comment or
#: processing instruction ahead of it. Anchoring here rather than "everything
#: before the first `>`" matters: Penpot re-serialises a document with an XML
#: declaration on the way back in, and partitioning on `>` would then hand
#: back the declaration and silently scale nothing.
_ROOT_SVG = re.compile(rb"\A(?:\s|<\?[^>]*\?>|<!--.*?-->)*<svg\b[^>]*>", re.S)

#: The root's presentation width/height. The leading whitespace is load-
#: bearing -- `\b` also matches after a hyphen, so `stroke-width="4"` on the
#: root would be scaled as if it were the width.
_ROOT_DIM = re.compile(rb'(?P<attr>\s(?:width|height)=")(?P<value>[\d.]+)"')


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
    root = _ROOT_SVG.match(svg)
    if root is None:
        return svg

    def rewrite(match: "re.Match[bytes]") -> bytes:
        value = float(match.group("value")) * factor
        # Never `:g`: it switches to scientific notation past six digits, and
        # `width="1.39e+06"` is not a valid SVG length -- the browser renders
        # nothing at all. `renderspec` clamps the factor so the product stays
        # in a sane range; this keeps the spelling plain regardless.
        text = f"{value:.6f}".rstrip("0").rstrip(".") or "0"
        return match.group("attr") + text.encode() + b'"'

    return _ROOT_DIM.sub(rewrite, root.group(0)) + svg[root.end():]


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
                # The fetcher is expected to have vetted this already, but the
                # value lands inside an attribute in a document served to every
                # viewer, so it is checked again where the splice happens
                # rather than trusted across a callable boundary.
                if not _MEDIA_TYPE.match(content_type or ""):
                    problems.append(
                        f"could not inline {url}: content-type "
                        f"{content_type!r} is not a bare type/subtype")
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

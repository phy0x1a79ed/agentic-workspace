"""The view URL's parameters, and the XML transforms they name.

A page view is addressed by a URL, and everything that makes one placement
differ from another rides in that URL's query string. This module is the single
authority for it: the prefix, the parameter grammar, the canonical spelling, and
the transforms each parameter performs on the document. Every surface — the HTTP
handler, ``view_url``, an autopublish link, the export-time inliner — goes
through the parser and the formatter here, which is what stops a parameter from
existing on one surface and silently not on another.

Two parameters today.

**``swap=<from>:<to>``**, repeatable. Replaces one colour with another
everywhere it is used as a colour. Draw the variable region of a page in a mask
colour nobody would pick on purpose — "missing texture" magenta, ``#ff00ff`` —
and one page serves every colour its consumers ask for, instead of a
near-duplicate page per colour.

Replacement is **simultaneous**, not sequential: ``swap=aa0000:bb0000`` and
``swap=bb0000:cc0000`` in one URL map each source to its own target and never
chain, because a single alternation is compiled over all the sources and a
regex never rescans what it just wrote.

What a swap reaches is deliberately narrow. Style values whose key ends in
``Color`` (plus drawio's two colour keys that do not, ``imageBorder`` and
``imageBackground``), the colour spellings inside a cell's label, and the text
of referenced ``/files/**.svg`` images. It does **not** reach an ``image=``
value — that is how an inlined data URI survives the rewrite untouched, and a
blanket text replace is ruled out for exactly that reason. Raster images cannot
be masked at all: a pink region inside a PNG stays pink, which is a property of
the format rather than a gap to be closed. Named colours (``red``), ``rgb()``
notation and eight-digit ``#rrggbbaa`` are out of scope; eight-digit values are
rejected at parse time rather than half-supported, because accepting them makes
the width rule ambiguous.

**``crop=<name>``**. Renders only the region covered by the shape on that page
whose label — or, failing that, whose id — is ``<name>``. Draw a rectangle
around what you want, name it, and one page serves several framings. The frame
is restyled to a bare outline before rendering (so it is guaranteed to draw, and
therefore to be measurable) and then removed from the output entirely, which is
why an author does not have to remember to make it invisible.

The parameter order in a URL never matters: both the formatter and the
fingerprint sort, so two callers asking for the same variant produce the same
URL string, the same cache path and the same ETag.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Iterable, Mapping
from urllib.parse import quote

from . import xmlmodel

#: A separate prefix, nested under the editor's ``/drawio-app`` static mount.
#: Lives here rather than in :mod:`awm.drawio.view` so that :mod:`awm.drawio
#: .export` can recognise a view reference without importing the view layer,
#: which imports it. ``view`` re-exports this name.
VIEW_PREFIX = os.environ.get("DRAWIO_VIEW_PREFIX", "/drawio-app/view")

#: Cap on swaps in one request. The URL space is caller-controlled and each
#: source contributes alternatives to a compiled pattern; a bound keeps a
#: hand-written URL from turning into a pathological regex.
MAX_SWAPS = 24

#: Style keys whose value is a colour but whose name does not end in ``Color``.
EXTRA_COLOUR_KEYS = frozenset({"imageBorder", "imageBackground"})

#: What the crop frame is restyled to before rendering: an outline, so drawio
#: definitely draws it and the browser can measure it. It is deleted from the
#: SVG after measuring, so nothing about this style reaches the output.
CROP_FRAME_STYLE = "rounded=0;html=1;fillColor=none;strokeColor=#000000;strokeWidth=1;"

#: Attributes carrying a cell's text, in the order drawio uses them.
_LABEL_ATTRS = ("value", "label")

#: Elements that can be a cell. ``object``/``UserObject`` wrap an ``mxCell`` and
#: carry the id and label themselves.
_CELL_TAGS = ("mxCell", "object", "UserObject")

_HEX6 = re.compile(r"^[0-9a-f]{6}$")
_HEX3 = re.compile(r"^[0-9a-f]{3}$")


class SpecError(ValueError):
    """A view parameter could not be understood. Always names the token."""


class CropNotFound(ValueError):
    """No shape on the target page carries the requested crop name."""


@dataclass(frozen=True)
class RenderSpec:
    """Everything a view URL can say about *how* to render, canonicalised.

    Frozen and hashable so it can key a cache without anybody having to
    remember to copy it.
    """

    scale: float = 1.0
    #: ``((from, to), …)``, canonical six-digit lowercase hex, sorted.
    swaps: tuple[tuple[str, str], ...] = ()
    crop: str | None = None

    @property
    def is_plain(self) -> bool:
        """Whether this asks for exactly what an un-parameterised URL asks for.

        The plain case keeps the pre-parameter cache path and content key, so
        deploying this does not throw away a single already-rendered page.
        """
        return self.scale == 1.0 and not self.swaps and self.crop is None


DEFAULT = RenderSpec()


# --- parsing ---------------------------------------------------------------

def canonical_colour(token: str) -> str:
    """``#F0F`` / ``%23ff00ff`` / ``ff00ff`` → ``ff00ff``.

    A leading ``#`` and its percent-encoded spelling are both tolerated:
    ``parse_qs`` decodes once, so which one arrives depends on how carefully
    the caller encoded the URL, and refusing one of them would be a papercut
    with no upside.
    """
    raw = (token or "").strip()
    text = raw
    for prefix in ("%23", "#"):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):]
            break
    text = text.lower()
    if _HEX3.match(text):
        return "".join(c * 2 for c in text)
    if _HEX6.match(text):
        return text
    if re.fullmatch(r"[0-9a-f]{8}", text):
        raise SpecError(
            f"colour {raw!r} is eight-digit #rrggbbaa, which is not supported; "
            "use the six-digit form and set opacity in the diagram"
        )
    raise SpecError(
        f"colour {raw!r} is not a 3- or 6-digit hex colour (e.g. ff00ff); "
        "named colours and rgb() notation are not supported"
    )


def parse_swap(token: str) -> tuple[str, str]:
    """``ff00ff:00aa55`` → ``("ff00ff", "00aa55")``."""
    raw = (token or "").strip()
    if not raw:
        raise SpecError("empty swap= parameter; expected swap=<from>:<to>")
    if raw.count(":") != 1:
        raise SpecError(
            f"swap {raw!r} is not <from>:<to> (e.g. swap=ff00ff:00aa55)")
    left, right = raw.split(":", 1)
    return canonical_colour(left), canonical_colour(right)


def parse_swaps(tokens: Iterable[str]) -> tuple[tuple[str, str], ...]:
    """Canonicalise, reject duplicate sources, and sort.

    A duplicate source is refused rather than resolved by a last-wins rule:
    ``swap=ff00ff:red&swap=ff00ff:blue`` has no defensible reading, and picking
    one silently is the class of failure this service exists to avoid.
    """
    pairs: dict[str, str] = {}
    for token in tokens:
        source, target = parse_swap(token)
        if source in pairs and pairs[source] != target:
            raise SpecError(
                f"colour {source!r} is swapped twice, to {pairs[source]!r} and "
                f"{target!r}; a source colour may only be swapped once")
        pairs[source] = target
    if len(pairs) > MAX_SWAPS:
        raise SpecError(f"{len(pairs)} swaps requested; the limit is {MAX_SWAPS}")
    return tuple(sorted(pairs.items()))


def from_query(query: Mapping[str, list[str]]) -> RenderSpec:
    """Read a spec out of a ``parse_qs`` mapping.

    Unknown parameters are ignored on purpose — the consumer client appends a
    cache-buster, and ``rev`` selects a revision rather than describing a
    render, so neither may perturb the variant.

    ``scale`` keeps its long-standing tolerant fallback (a junk value renders at
    1.0 rather than failing an image request). ``swap`` and ``crop`` do not:
    they change what the picture *means*, so a malformed one is an error the
    caller has to see. The handler must therefore parse with
    ``keep_blank_values=True`` — otherwise ``?swap=`` is discarded before this
    can complain about it, which is precisely the silent degradation the rest
    of this service refuses.
    """
    scale = 1.0
    raw_scale = (query.get("scale") or [None])[0]
    if raw_scale:
        try:
            scale = float(raw_scale)
        except (TypeError, ValueError):
            scale = 1.0

    swaps = parse_swaps(query.get("swap") or ())

    crop = None
    raw_crop = query.get("crop")
    if raw_crop is not None:
        crop = (raw_crop[0] or "").strip()
        if not crop:
            raise SpecError("empty crop= parameter; expected crop=<shape name>")

    return RenderSpec(scale=scale, swaps=swaps, crop=crop)


# --- formatting ------------------------------------------------------------

def to_query(spec: RenderSpec) -> str:
    """The canonical query string for a spec, without the leading ``?``.

    Colons are left unencoded: they are legal in a query and a readable
    ``swap=ff00ff:00aa55`` is the whole point of choosing this syntax. Sorted,
    so the same variant always spells itself the same way — two callers
    producing different strings would defeat the browser's cache as surely as a
    different render would.
    """
    parts: list[str] = []
    if spec.scale != 1.0:
        parts.append(f"scale={spec.scale:g}")
    for source, target in spec.swaps:
        parts.append(f"swap={source}:{target}")
    if spec.crop:
        parts.append(f"crop={quote(spec.crop, safe='')}")
    return "&".join(parts)


def fingerprint(spec: RenderSpec) -> str:
    """A short, filesystem-safe name for this variant.

    ``__plain__`` for an un-parameterised render, which is what keeps the plain
    case on its original cache path.
    """
    if spec.is_plain:
        return "__plain__"
    return hashlib.sha256(to_query(spec).encode("utf-8")).hexdigest()[:16]


def describe(spec: RenderSpec) -> str:
    """Human-readable, for a log line or an error message."""
    if spec.is_plain:
        return "plain"
    bits = []
    if spec.scale != 1.0:
        bits.append(f"scale {spec.scale:g}")
    if spec.swaps:
        bits.append(", ".join(f"#{a}->#{b}" for a, b in spec.swaps))
    if spec.crop:
        bits.append(f"crop {spec.crop!r}")
    return "; ".join(bits)


# --- colour substitution ---------------------------------------------------

def _spellings(colour: str) -> list[str]:
    """Every hex spelling that denotes this colour: ``ff00ff`` and ``f0f``."""
    out = [colour]
    if colour[0] == colour[1] and colour[2] == colour[3] and colour[4] == colour[5]:
        out.append(colour[0] + colour[2] + colour[4])
    return out


class _Substituter:
    """One compiled alternation over every source colour, applied in one pass.

    Simultaneity falls out of this: ``re.sub`` never rescans its own output, so
    a colour written by one swap can never be read by another. The trailing
    lookahead stops a six-digit match from eating the first six digits of an
    eight-digit value, and lets a three-digit source decline ``#abcdef``.
    """

    def __init__(self, swaps: tuple[tuple[str, str], ...]):
        self.swaps = swaps
        self._lookup: dict[str, str] = {}
        alternatives: list[str] = []
        for source, target in swaps:
            for spelling in _spellings(source):
                alternatives.append(spelling)
                self._lookup[spelling] = target
        alternatives.sort(key=len, reverse=True)
        self._pattern = re.compile(
            "#(" + "|".join(alternatives) + r")(?![0-9a-fA-F])",
            re.IGNORECASE) if alternatives else None
        self.hits = 0

    def __bool__(self) -> bool:
        return self._pattern is not None

    def sub(self, text: str) -> str:
        if self._pattern is None or not text:
            return text

        def _one(match: re.Match) -> str:
            self.hits += 1
            return "#" + self._lookup[match.group(1).lower()]

        return self._pattern.sub(_one, text)


def _is_colour_key(key: str) -> bool:
    return key.lower().endswith("color") or key in EXTRA_COLOUR_KEYS


def _swap_style(style: str, sub: _Substituter) -> str:
    """Rewrite only the colour-valued segments of a drawio style string.

    Segment-wise rather than whole-string: an ``image=`` value can be a
    megabyte of inlined data and must come through byte-identical, and the key
    test is the mechanism that guarantees it. Empty segments and bare tokens are
    preserved so the style comes back spelled the way it went in.
    """
    parts = style.split(";")
    for index, segment in enumerate(parts):
        if "=" not in segment:
            continue
        key, value = segment.split("=", 1)
        if not _is_colour_key(key):
            continue
        replaced = sub.sub(value)
        if replaced != value:
            parts[index] = f"{key}={replaced}"
    return ";".join(parts)


def swap_text(text: str, swaps: tuple[tuple[str, str], ...]) -> tuple[str, int]:
    """Substitute colours in free text — a referenced SVG's source, say.

    Returns the rewritten text and the number of replacements, because a swap
    that matched nothing is not an error (a mask may live on one page and not
    another) but must not be silent either.
    """
    if not swaps:
        return text, 0
    sub = _Substituter(swaps)
    return sub.sub(text), sub.hits


def swap_document(xml: str, swaps: tuple[tuple[str, str], ...]) -> tuple[str, int]:
    """Substitute colours in a ``.drawio`` document, structurally.

    Raises :class:`awm.drawio.xmlmodel.CompressedDiagram` for a deflated page:
    a compressed diagram renders fine but cannot be rewritten, and returning an
    un-swapped render would look exactly like a swap that matched nothing.
    """
    if not swaps:
        return xml, 0
    root = xmlmodel.parse(xml)
    sub = _Substituter(swaps)
    for element in root.iter():
        style = element.get("style")
        if style:
            replaced = _swap_style(style, sub)
            if replaced != style:
                element.set("style", replaced)
        for attr in _LABEL_ATTRS:
            value = element.get(attr)
            if value:
                replaced = sub.sub(value)
                if replaced != value:
                    element.set(attr, replaced)
    return xmlmodel.serialize(root), sub.hits


# --- crop ------------------------------------------------------------------

def _cells(diagram) -> Iterable:
    model_root = diagram.find("mxGraphModel/root")
    if model_root is None:
        return ()
    return (el for el in model_root.iter() if el.tag in _CELL_TAGS)


def prepare_crop(xml: str, index: int | None, name: str) -> tuple[str, str]:
    """Find the named frame, make it measurable, and return ``(xml, cell id)``.

    Name resolution happens here rather than in the browser so an unknown name
    is an honest 404 naming what was asked for, instead of a render that
    silently ignores the parameter. Label wins over id — a name typed into a
    shape is what an author will reach for — but an id is accepted so a shape
    that must stay unlabelled can still be a frame.

    The frame is restyled to a bare outline rather than hidden: drawio builds
    no state for an invisible cell, so a hidden frame is a frame the browser
    cannot measure. It is deleted from the SVG after measuring, so this style
    never reaches the output.
    """
    root = xmlmodel.parse(xml)
    diagrams = root.findall("diagram")
    if index is None:
        targets = diagrams
    elif 0 <= index < len(diagrams):
        targets = [diagrams[index]]
    else:  # pragma: no cover — page_index resolved it a moment ago
        raise CropNotFound(f"page index {index} is out of range")

    by_label, by_id = None, None
    for diagram in targets:
        for cell in _cells(diagram):
            if by_label is None:
                for attr in _LABEL_ATTRS:
                    if (cell.get(attr) or "").strip() == name:
                        by_label = cell
                        break
            if by_id is None and cell.get("id") == name:
                by_id = cell
    # `or` would be wrong here: an element with no children is falsy, so a
    # childless labelled cell would silently lose to an id match.
    frame = by_label if by_label is not None else by_id
    if frame is None:
        raise CropNotFound(
            f"no shape named {name!r} on this page to crop to; label a "
            "rectangle with that name (or use its cell id)")

    cell_id = frame.get("id")
    if not cell_id:  # pragma: no cover — drawio always writes an id
        raise CropNotFound(f"the shape named {name!r} has no id to crop by")

    holder = frame.find("mxCell") if frame.tag in ("object", "UserObject") else frame
    if holder is None:  # pragma: no cover — an object always wraps an mxCell
        holder = frame
    holder.set("style", CROP_FRAME_STYLE)
    holder.attrib.pop("visible", None)
    for attr in _LABEL_ATTRS:
        if frame.get(attr):
            frame.set(attr, "")
    return xmlmodel.serialize(root), cell_id

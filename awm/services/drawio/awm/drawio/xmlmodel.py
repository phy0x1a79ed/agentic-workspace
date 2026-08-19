"""Canonical serialization for ``.drawio`` files — the foundation the merge
story rests on.

A ``.drawio`` file is an ``mxfile`` holding one ``diagram`` per page, each
wrapping an ``mxGraphModel``/``root`` of ``mxCell`` elements. Two writers
produce it: the browser (drawio's own serializer, via ``getFileData``) and
this workspace's script layer (``dwg.py``, via ``ElementTree``). They agree on
*content* but disagree on *spelling* — and git merges text, so every spelling
difference is a phantom conflict.

Measured on the real 5.8 MB ``prokaryotic_metabolism.drawio``, the differences
that matter are:

* **Viewport churn.** ``mxGraphModel/@dx,@dy`` is the scroll offset. It changes
  when anyone scrolls or switches pages — and it sits on the line that *opens*
  each page's content, the worst possible place for a spurious hunk. Four of
  nine pages differed on nothing else between two consecutive saves.
* **Float noise.** Coordinates accumulate binary-float error through drag and
  layout operations; the file literally contains ``329.9999999999999``.
* **Attribute order.** Browser-created pages spell ``<diagram id=… name=…>``,
  script-created ones ``<diagram name=… id=…>``. XML attribute order carries no
  meaning, so both are correct and neither is stable.
* **Identity churn.** ``mxfile/@host``, ``@agent`` and ``@version`` record which
  browser last saved — pure noise that rewrites line 1 on every save.

What is deliberately *not* normalized: **sibling order**. In drawio, the order
of ``mxCell`` siblings under ``root`` is the z-order, and the order of
``mxPoint`` under ``Array`` is the waypoint sequence. Sorting either would be a
silent render change, which is exactly the failure mode normalization exists to
prevent. Canonicalization here is strictly attribute-level plus whitespace.

Compressed diagrams are rejected rather than normalized: a deflated payload is
one opaque base64 blob, so every downstream consumer — diff, merge, history,
the cell reader — degenerates to whole-file replacement.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

# --- what gets neutralized -------------------------------------------------

#: Scroll offset on each page's model element. Viewport, not content.
VIEWPORT_ATTRS: frozenset[str] = frozenset({"dx", "dy"})

#: Which browser/agent/version last touched the file, plus mtime-ish fields
#: some drawio builds emit. All rewrite line 1 on every save and mean nothing.
MXFILE_VOLATILE: frozenset[str] = frozenset(
    {"host", "agent", "version", "modified", "etag", "type", "scale", "border"}
)

#: Elements whose x/y/width/height are geometry, and therefore subject to
#: binary-float noise. Restricted by element name on purpose — an ``id`` or a
#: ``value`` that happens to parse as a float must not be rewritten.
COORD_ELEMENTS: frozenset[str] = frozenset({"mxGeometry", "mxPoint", "mxRectangle"})
COORD_ATTRS: frozenset[str] = frozenset({"x", "y", "width", "height"})

#: Decimal places kept for coordinates. drawio's own grid is 10px and its UI
#: shows integers; 4 places is far below any visible difference and comfortably
#: above the ~1e-13 noise floor observed in the file.
COORD_PRECISION = 4

#: Attributes emitted first, in this order, when present. Everything else
#: follows alphabetically. Purely for human readability of diffs — the ordering
#: only has to be *deterministic* to do its job.
ATTR_PRIORITY: tuple[str, ...] = (
    "id", "name", "value", "style",
    "vertex", "edge", "connectable", "collapsed", "visible",
    "parent", "source", "target",
    "x", "y", "width", "height",
    "as", "relative",
)
_PRIORITY_INDEX = {k: i for i, k in enumerate(ATTR_PRIORITY)}

INDENT = "  "


class CompressedDiagram(ValueError):
    """A ``<diagram>`` holds a deflated payload instead of an ``mxGraphModel``."""


class MalformedDiagram(ValueError):
    """The bytes are not a usable ``.drawio`` document."""


# --- parsing ---------------------------------------------------------------

def parse(xml: str | bytes) -> ET.Element:
    """Parse ``.drawio`` bytes into an ``mxfile`` element.

    Raises :class:`MalformedDiagram` if the document is not XML or its root is
    not ``mxfile``, and :class:`CompressedDiagram` if any page is deflated.
    """
    if isinstance(xml, bytes):
        xml = xml.decode("utf-8")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise MalformedDiagram(f"not well-formed XML: {exc}") from exc
    if root.tag != "mxfile":
        raise MalformedDiagram(f"root element is <{root.tag}>, expected <mxfile>")
    for diagram in root.findall("diagram"):
        if diagram.find("mxGraphModel") is None:
            payload = (diagram.text or "").strip()
            if payload:
                raise CompressedDiagram(
                    f"page {diagram.get('name') or diagram.get('id') or '?'!r} is "
                    "compressed; save it uncompressed (Extras > Edit Diagram, or "
                    "getFileData(..., uncompressed=True))"
                )
            raise MalformedDiagram(
                f"page {diagram.get('name') or diagram.get('id') or '?'!r} has no "
                "<mxGraphModel>"
            )
    return root


# --- attribute canonicalization --------------------------------------------

def _canon_number(raw: str) -> str:
    """Round a coordinate to :data:`COORD_PRECISION` and spell it minimally.

    Non-numeric and non-finite values pass through untouched — a normalizer
    that silently rewrites something it does not understand is worse than one
    that leaves it alone.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return raw
    if not math.isfinite(value):
        return raw
    value = round(value, COORD_PRECISION)
    # Fixed-point on purpose: repr() flips to exponent notation for large
    # magnitudes, which would make the spelling depend on the value's size.
    text = f"{value:.{COORD_PRECISION}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _canon_attrs(elem: ET.Element) -> list[tuple[str, str]]:
    """Return this element's attributes, filtered, rounded, and ordered."""
    attrs = dict(elem.attrib)

    if elem.tag == "mxfile":
        for key in MXFILE_VOLATILE:
            attrs.pop(key, None)
    elif elem.tag == "mxGraphModel":
        for key in VIEWPORT_ATTRS:
            attrs.pop(key, None)

    if elem.tag in COORD_ELEMENTS:
        for key in COORD_ATTRS & attrs.keys():
            attrs[key] = _canon_number(attrs[key])

    return sorted(
        attrs.items(),
        key=lambda kv: (_PRIORITY_INDEX.get(kv[0], len(ATTR_PRIORITY)), kv[0]),
    )


def normalize_tree(mxfile: ET.Element) -> ET.Element:
    """Canonicalize in place and return the same element.

    Only ``mxfile/@pages`` is *written*: it is a cached count of ``<diagram>``
    children, and a stale one from either writer would diff for no reason.
    """
    mxfile.set("pages", str(len(mxfile.findall("diagram"))))
    return mxfile


# --- serialization ---------------------------------------------------------

_ATTR_ESCAPES = {
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
    "\n": "&#10;", "\r": "&#13;", "\t": "&#9;",
}
_ATTR_RE = re.compile("[" + "".join(map(re.escape, _ATTR_ESCAPES)) + "]")
_TEXT_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}
_TEXT_RE = re.compile("[&<>]")


def _esc_attr(value: str) -> str:
    return _ATTR_RE.sub(lambda m: _ATTR_ESCAPES[m.group()], value)


def _esc_text(value: str) -> str:
    return _TEXT_RE.sub(lambda m: _TEXT_ESCAPES[m.group()], value)


def _write(elem: ET.Element, out: list[str], depth: int) -> None:
    pad = INDENT * depth
    attrs = "".join(f' {k}="{_esc_attr(v)}"' for k, v in _canon_attrs(elem))
    children = list(elem)
    text = (elem.text or "").strip()

    if not children and not text:
        out.append(f"{pad}<{elem.tag}{attrs} />\n")
        return
    if not children:
        out.append(f"{pad}<{elem.tag}{attrs}>{_esc_text(text)}</{elem.tag}>\n")
        return

    out.append(f"{pad}<{elem.tag}{attrs}>\n")
    if text:
        # Mixed content is not a shape drawio produces, but preserving it beats
        # dropping it silently.
        out.append(f"{pad}{INDENT}{_esc_text(text)}\n")
    for child in children:
        _write(child, out, depth + 1)
    out.append(f"{pad}</{elem.tag}>\n")


def serialize(mxfile: ET.Element) -> str:
    """Serialize a (normalized) tree to canonical text with a trailing newline."""
    out: list[str] = []
    _write(mxfile, out, 0)
    return "".join(out)


def normalize(xml: str | bytes) -> str:
    """Parse, canonicalize and re-serialize. Idempotent by construction."""
    return serialize(normalize_tree(parse(xml)))


def as_tree(xml: "str | bytes | ET.Element") -> ET.Element:
    """Parse, or pass an already-parsed ``mxfile`` straight through.

    A caller that needs both a page index and a page cut would otherwise parse
    a half-megabyte document twice to do it.
    """
    return xml if isinstance(xml, ET.Element) else parse(xml)


def single_page(xml: "str | bytes | ET.Element", index: int) -> str:
    """The document reduced to the one page at ``index``, still an ``mxfile``.

    The root's attributes come along, so the page keeps whatever document-level
    settings it was authored under. The source tree is left alone.
    """
    mxfile = as_tree(xml)
    diagrams = mxfile.findall("diagram")
    if not 0 <= index < len(diagrams):
        raise MalformedDiagram(
            f"page index {index} is out of range for a {len(diagrams)}-page "
            "document")
    cut = ET.Element(mxfile.tag, dict(mxfile.attrib))
    cut.append(diagrams[index])
    return serialize(normalize_tree(cut))


# --- inspection ------------------------------------------------------------

def page_summaries(mxfile: ET.Element) -> list[dict]:
    """Per-page ``{id, name, cells}`` — what the reception page lists.

    ``cells`` excludes the two structural root cells (``0`` and its child
    ``1``), so an empty page reports 0 rather than 2.
    """
    summaries = []
    for diagram in mxfile.findall("diagram"):
        root = diagram.find("mxGraphModel/root")
        cells = root.findall("mxCell") if root is not None else []
        structural = sum(1 for c in cells if c.get("id") in ("0", "1"))
        summaries.append({
            "id": diagram.get("id"),
            "name": diagram.get("name"),
            "cells": len(list(root)) - structural if root is not None else 0,
        })
    return summaries

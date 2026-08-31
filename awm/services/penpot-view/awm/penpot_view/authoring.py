"""Penpot shapes and change vectors, as plain Python values.

Everything here is pure: it builds the maps and vectors that
``update-file`` accepts and opens no socket. :mod:`awm.penpot_view.demo` is
the caller that puts them on the wire.

Two things about the wire format are not guessable from the JSON, and they
are why this module exists rather than the callers writing dicts inline.

**A shape must arrive as a record, not as a map.** Penpot's ``:add-obj``
handler runs the object through ``cts/valid-shape?``, whose first predicate
is an ``instance?`` check against the ``Shape`` record. Transit's tag
dispatch is the only thing that turns a JSON payload into a record on the
far side, so a shape is a :class:`~awm.penpot_view.exporter_client.Tagged`
``shape``, with ``rect`` for its selrect, ``point`` for each of its four
corners and ``matrix`` for its two transforms nested inside. A plain map
gets a 400 naming ``:data-validation`` and nothing else.

**Geometry is stated four times and must agree.** ``x/y/width/height``, the
selrect, the four corner points and the transform pair all describe the same
box. Penpot does not derive any of them from the others on this path -- the
frontend computes them together and sends the result -- so
:func:`shape` computes all four from one rectangle rather than letting a
caller supply them separately and get them out of step. A shape whose
selrect disagrees with its ``x/y`` renders in one place and hit-tests in
another, and nothing reports it.
"""

from __future__ import annotations

import uuid
from typing import Any

from .exporter_client import Keyword, Tagged

__all__ = [
    "kmap", "shape", "solid_fill", "image_fill", "text_content",
    "text_position", "add_obj", "mod_obj", "set_op", "add_page", "mod_page",
    "add_component",
]


def kmap(**attrs: Any) -> dict[Keyword, Any]:
    """A transit map with keyword keys, written in Python spelling.

    ``kmap(font_size="16")`` is ``{:font-size "16"}``. Every Penpot attribute
    is kebab-case and every Python keyword argument is snake_case, so the
    translation is total and mechanical -- which is the point: a hand-written
    ``Keyword("font-size")`` is one typo away from a silently ignored
    attribute, because an unknown key on a non-closed schema is accepted and
    dropped.
    """
    return {Keyword(k.replace("_", "-")): v for k, v in attrs.items()}


#: No rotation, no scale, no translation. Penpot stores the transform and its
#: inverse separately and both are required on every shape; for an unrotated
#: box they are both this.
IDENTITY = Tagged("matrix", kmap(a=1, b=0, c=0, d=1, e=0, f=0))


def _selrect(x: float, y: float, w: float, h: float) -> Tagged:
    return Tagged("rect", kmap(x=x, y=y, width=w, height=h,
                               x1=x, y1=y, x2=x + w, y2=y + h))


def _corners(x: float, y: float, w: float, h: float) -> list[Tagged]:
    """The four corners, clockwise from top-left -- Penpot's own order."""
    return [Tagged("point", kmap(x=px, y=py))
            for px, py in ((x, y), (x + w, y), (x + w, y + h), (x, y + h))]


def shape(*, id: uuid.UUID, name: str, type: str,
          x: float, y: float, width: float, height: float,
          parent_id: uuid.UUID, frame_id: uuid.UUID, **attrs: Any) -> Tagged:
    """One shape, with its geometry stated the four ways Penpot wants it."""
    body = kmap(
        id=id, name=name, type=Keyword(type),
        x=x, y=y, width=width, height=height,
        selrect=_selrect(x, y, width, height),
        points=_corners(x, y, width, height),
        transform=IDENTITY, transform_inverse=IDENTITY,
        parent_id=parent_id, frame_id=frame_id,
        rotation=0, flip_x=None, flip_y=None,
    )
    body.update(kmap(**attrs))
    return Tagged("shape", body)


def solid_fill(color: str, opacity: float = 1) -> dict[Keyword, Any]:
    return kmap(fill_color=color, fill_opacity=opacity)


def image_fill(*, id: uuid.UUID, width: int, height: int, mtype: str,
               name: str | None = None) -> dict[Keyword, Any]:
    """A fill that references an uploaded file-media-object.

    ``ImageColor`` is a *closed* schema -- width, height, mtype, id, name and
    keep-aspect-ratio, nothing else -- so this takes the media object's
    fields one at a time instead of forwarding the whole row that
    ``upload-file-media-object`` answers with.
    """
    img = kmap(id=id, width=width, height=height, mtype=mtype)
    if name:
        img.update(kmap(name=name))
    return kmap(fill_image=img)


def text_content(text: str, *, font_id: str, font_family: str,
                 font_variant_id: str = "regular", font_size: str = "16",
                 font_weight: str = "400", font_style: str = "normal",
                 fills: list | None = None) -> dict[Keyword, Any]:
    """The nested root/paragraph-set/paragraph/leaf tree a text shape stores.

    ``font-id`` and ``font-variant-id`` are what make the render fetch the
    font: the exporter's page walks the *content* tree collecting those two
    fields and loads each pair before it renders. ``font-family`` alone names
    a CSS family that was never fetched, which renders in whatever the
    headless browser falls back to -- legible, wrong, and silent.
    """
    leaf_attrs = kmap(font_id=font_id, font_family=font_family,
                      font_variant_id=font_variant_id, font_size=font_size,
                      font_weight=font_weight, font_style=font_style,
                      fills=fills if fills is not None else [solid_fill("#000000")])
    leaf = kmap(text=text)
    leaf.update(leaf_attrs)
    paragraph = kmap(type="paragraph", children=[leaf])
    paragraph.update(leaf_attrs)
    return kmap(type="root",
                children=[kmap(type="paragraph-set", children=[paragraph])])


def text_position(text: str, *, x: float, y: float, width: float,
                  height: float, font_family: str, font_size: str = "16",
                  font_weight: str = "400", font_style: str = "normal",
                  fills: list | None = None) -> list[dict[Keyword, Any]]:
    """The laid-out runs a text shape renders from, as a one-run vector.

    Penpot's editor measures the text in a browser and stores the result
    here. Nothing outside a browser can reproduce that measurement, and this
    does not try: it states one run covering the whole box. That is enough
    because the renderer emits ``textLength`` with
    ``lengthAdjust="spacingAndGlyphs"``, so the glyphs are stretched to the
    width given rather than overflowing it.

    Supplying it at all is what keeps the render inside plain SVG ``<text>``.
    A text shape with no position data still renders, but through a
    ``foreignObject`` carrying HTML, which a browser is free to ignore when
    the SVG is the ``src`` of an ``<img>`` -- which is exactly how the vault
    note embeds it.

    ``y`` is the baseline, not the top: the renderer sets
    ``dominant-baseline: ideographic``.
    """
    return [kmap(x=x, y=y, width=width, height=height,
                 text=text, font_family=font_family, font_size=font_size,
                 font_weight=font_weight, font_style=font_style,
                 fills=fills if fills is not None else [solid_fill("#000000")])]


# --- changes ---------------------------------------------------------------
#
# A change's `:type` is a *keyword*, and so is an operation's `:type` and its
# `:attr`. A text node's `:type` is a plain *string* -- `schema:content`
# spells it `[:= "root"]`. Both are load-bearing and they look identical in
# Python, which is why each one is written out here rather than left to a
# caller to remember.

def add_obj(obj: Tagged, *, id: uuid.UUID, page_id: uuid.UUID,
            frame_id: uuid.UUID, parent_id: uuid.UUID | None = None,
            ignore_touched: bool = False) -> dict[Keyword, Any]:
    """Insert one shape.

    The parent's ``:shapes`` vector is updated by the handler, so a board and
    then its children in that order is all it takes -- there is no separate
    change to register a child with its parent, and adding one would insert
    the id twice.
    """
    change = kmap(type=Keyword("add-obj"), id=id, obj=obj, page_id=page_id,
                  frame_id=frame_id,
                  parent_id=parent_id if parent_id is not None else frame_id)
    if ignore_touched:
        change.update(kmap(ignore_touched=True))
    return change


def set_op(attr: str, val: Any, *, ignore_touched: bool = False
           ) -> dict[Keyword, Any]:
    op = kmap(type=Keyword("set"), attr=Keyword(attr), val=val)
    if ignore_touched:
        op.update(kmap(ignore_touched=True))
    return op


def mod_obj(id: uuid.UUID, *, page_id: uuid.UUID,
            operations: list) -> dict[Keyword, Any]:
    return kmap(type=Keyword("mod-obj"), id=id, page_id=page_id, operations=operations)


def add_page(id: uuid.UUID, name: str) -> dict[Keyword, Any]:
    """A new empty page. Never send ``:page`` beside ``:id``/``:name`` --
    the handler raises ``:conflict`` if both arrive."""
    return kmap(type=Keyword("add-page"), id=id, name=name)


def mod_page(id: uuid.UUID, *, name: str) -> dict[Keyword, Any]:
    return kmap(type=Keyword("mod-page"), id=id, name=name)


def add_component(id: uuid.UUID, *, name: str, path: str,
                  main_instance_id: uuid.UUID,
                  main_instance_page: uuid.UUID) -> dict[Keyword, Any]:
    """Register a component over a board that already exists.

    ``id`` is a **new** uuid, never the board's. The board is named by
    ``main-instance-id`` and separately marked up with a ``:mod-obj``
    carrying ``:component-id``, ``:component-file``, ``:component-root`` and
    ``:main-instance`` -- see :func:`main_instance_ops`. Setting the
    component id equal to the board id produces a file that opens, looks
    right, and never syncs a copy, and no validation catches it.
    """
    return kmap(type=Keyword("add-component"), id=id, name=name, path=path,
                main_instance_id=main_instance_id,
                main_instance_page=main_instance_page)


def main_instance_ops(*, component_id: uuid.UUID,
                      file_id: uuid.UUID) -> list[dict[Keyword, Any]]:
    """What marks a board as the main instance of a component."""
    return [set_op("component-id", component_id),
            set_op("component-file", file_id),
            set_op("component-root", True),
            set_op("main-instance", True),
            set_op("shape-ref", None),
            set_op("touched", None)]

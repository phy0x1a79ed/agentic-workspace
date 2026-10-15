"""``swap``/``crop`` post-processing of an exported Penpot board SVG.

The failures worth pinning here are the quiet ones. A swap implemented as a
chain of substitutions would turn a colour that was never asked for into one
that was -- pinned by ``test_a_swap_never_chains``. A swap that matches
nothing looks identical to a swap that silently failed unless the bytes are
checked -- pinned by ``test_a_swap_matching_nothing_is_byte_identical``. A
gradient stop, which Penpot spells as a bare ``stop-color="#hex"`` rather
than as ``rgb()`` inside ``style=``, must be swapped like any other colour
rather than silently left behind -- pinned by
``test_gradient_stops_are_swapped``. A crop
against an unknown shape must fail loudly rather than render the full,
un-cropped board -- pinned by ``test_crop_of_an_unknown_shape_is_refused``.
"""

from __future__ import annotations

import pytest

from awm.penpot_view import renderspec as R
from awm.penpot_view import svgpost as S

# A trimmed but faithful copy of the one confirmed live export: a board
# ("shape-<ROOT_ID>") that is its own frame, with the exporter's separate
# `screenshot-<uuid>` locator on the <svg> root, a `frame-clip` wrapper, and
# a single white-filled rect whose colour lives in style="fill: rgb(...)".
ROOT_ID = "00000000-0000-0000-0000-000000000000"

SAMPLE_SVG = (
    f'<svg width="400" xmlns="http://www.w3.org/2000/svg" height="300" '
    f'id="screenshot-{ROOT_ID}" viewBox="0 0 400 300" '
    f'xmlns:xlink="http://www.w3.org/1999/xlink" fill="none" version="1.1">'
    f'<g id="shape-{ROOT_ID}"><defs>'
    f'<clipPath id="frame-clip-{ROOT_ID}-render-1" class="frame-clip frame-clip-def">'
    f'<rect rx="0" ry="0" x="0" y="0" width="400" height="300" '
    f'transform="matrix(1.000000, 0.000000, 0.000000, 1.000000, 0.000000, 0.000000)"/>'
    f'</clipPath></defs><g class="frame-container-wrapper"><g class="frame-container-blur">'
    f'<g class="frame-container-shadows">'
    f'<g clip-path="url(#frame-clip-{ROOT_ID}-render-1)" fill="none">'
    f'<g class="fills" id="fills-{ROOT_ID}">'
    f'<rect width="400" height="300" class="frame-background" x="0" '
    f'transform="matrix(1.000000, 0.000000, 0.000000, 1.000000, 0.000000, 0.000000)" '
    f'style="fill: rgb(255, 255, 255); fill-opacity: 1;" ry="0" rx="0" y="0"/>'
    f'</g><g class="frame-children"/></g></g></g></g></g></svg>'
).encode("utf-8")


# --- swap ------------------------------------------------------------------

def test_a_single_swap_rewrites_rgb_inside_style():
    data, problems = S.swap_svg(SAMPLE_SVG, (("ffffff", "ff0000"),))
    assert b"rgb(255, 0, 0)" in data
    assert b"rgb(255, 255, 255)" not in data
    assert problems == []


def test_a_swap_never_chains():
    """`a -> b` and `b -> c` requested together must apply simultaneously: a
    shape that started as `a` must land on `b`, never fall through to `c`."""
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        b'<rect id="was-magenta" style="fill: rgb(255, 0, 255);"/>'
        b'<rect id="was-teal" style="fill: rgb(0, 170, 85);"/>'
        b"</svg>"
    )
    swaps = (("ff00ff", "00aa55"), ("00aa55", "112233"))
    data, problems = S.swap_svg(svg, swaps)
    text = data.decode("utf-8")
    # the originally-magenta rect landed on the first swap's target...
    assert 'id="was-magenta" style="fill: rgb(0, 170, 85);"' in text
    # ...and the originally-teal rect landed on the second swap's target...
    assert 'id="was-teal" style="fill: rgb(17, 34, 51);"' in text
    # ...but nothing ended up chained two hops from magenta to rgb(17, 34, 51).
    assert 'id="was-magenta" style="fill: rgb(17, 34, 51);"' not in text
    assert problems == []


def test_a_swap_matching_nothing_is_byte_identical():
    data, problems = S.swap_svg(SAMPLE_SVG, (("00ff00", "000000"),))
    assert data == SAMPLE_SVG
    assert problems == []


def test_a_bare_colour_attribute_is_swapped_too():
    """Both forms Penpot emits get rewritten. A colour left behind in one of
    them is a half-swapped picture that reports success -- the failure this
    module exists to refuse."""
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        b'<rect fill="#ff00ff" width="1" height="1"/>'
        b'<rect style="fill: rgb(255, 0, 255);" width="1" height="1"/>'
        b"</svg>"
    )
    data, problems = S.swap_svg(svg, (("ff00ff", "00aa55"),))
    text = data.decode("utf-8")
    assert "rgb(0, 170, 85)" in text
    assert 'fill="#00aa55"' in text
    assert 'fill="#ff00ff"' not in text
    assert problems == []


def test_gradient_stops_are_swapped():
    """Penpot writes a gradient stop as a bare `stop-color="#hex"`, not as
    `rgb()` inside style= -- verified against a live export of a board with
    two linear gradients. Rewriting only style= would leave every gradient at
    its original colour."""
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><defs>'
        b'<linearGradient id="g">'
        b'<stop offset="0" stop-color="#b1b2b5" stop-opacity="0"/>'
        b'<stop offset="1" stop-color="#b1b2b5" stop-opacity="1"/>'
        b"</linearGradient></defs></svg>"
    )
    data, problems = S.swap_svg(svg, (("b1b2b5", "ff0000"),))
    text = data.decode("utf-8")
    assert text.count('stop-color="#ff0000"') == 2
    assert "b1b2b5" not in text
    assert problems == []


def test_fill_none_is_left_alone_and_not_reported():
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" fill="none">'
           b'<rect fill="none"/></svg>')
    data, problems = S.swap_svg(svg, (("ff00ff", "00aa55"),))
    assert data == svg
    assert problems == []


def test_a_colour_spelling_this_module_cannot_parse_is_reported():
    """A CSS colour name is skipped by the rewrite. Skipping it silently
    would hand back an unchanged picture with a clean report."""
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg">'
           b'<rect fill="rebeccapurple"/></svg>')
    _, problems = S.swap_svg(svg, (("663399", "00aa55"),))
    assert len(problems) == 1
    assert "rebeccapurple" in problems[0]


def test_a_repeated_swap_source_is_still_one_lookup():
    """Sanity check that `swap_svg` trusts renderspec's canonicalisation
    rather than re-deriving it -- lower/upper or #-prefixed spellings never
    reach this module because `parse_swaps` already normalised them."""
    swaps = R.parse_swaps(["#FFFFFF:ff0000"])
    data, problems = S.swap_svg(SAMPLE_SVG, swaps)
    assert b"rgb(255, 0, 0)" in data
    assert problems == []


def test_no_swaps_is_a_no_op():
    data, problems = S.swap_svg(SAMPLE_SVG, ())
    assert data == SAMPLE_SVG
    assert problems == []


# --- crop --------------------------------------------------------------

def test_crop_rewrites_the_root_viewbox_width_and_height():
    data = S.crop_svg(SAMPLE_SVG, f"shape-{ROOT_ID}")
    root = data.decode("utf-8")
    assert 'viewBox="0 0 400 300"' in root
    assert 'width="400"' in root
    assert 'height="300"' in root


def test_crop_accepts_a_bare_shape_id_without_the_shape_prefix():
    data = S.crop_svg(SAMPLE_SVG, ROOT_ID)
    assert 'viewBox="0 0 400 300"' in data.decode("utf-8")


def test_crop_of_a_nested_frame_uses_its_own_frame_clip_rect():
    """Tier 1: a frame carries its own frame-clip rect, computed by Penpot as
    the frame's rendered boundary -- crop must use it verbatim, not the full
    board's bounds."""
    nested_id = "11111111-1111-1111-1111-111111111111"
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300" '
        f'viewBox="0 0 400 300">'
        f'<g id="shape-{ROOT_ID}">'
        f'<g id="shape-{nested_id}"><defs>'
        f'<clipPath id="frame-clip-{nested_id}-render-1">'
        f'<rect x="50" y="50" width="100" height="80" '
        f'transform="matrix(1,0,0,1,0,0)"/>'
        f"</clipPath></defs>"
        f"</g></g></svg>"
    ).encode("utf-8")
    data = S.crop_svg(svg, nested_id)
    root = data.decode("utf-8")
    assert 'viewBox="50 50 100 80"' in root
    assert 'width="100"' in root
    assert 'height="80"' in root


def test_crop_of_a_plain_shape_falls_back_to_its_rect_geometry():
    """Tier 2: a shape with no frame-clip of its own is cropped to the union
    of its <rect> descendants, transformed -- not the Penpot-model box."""
    shape_id = "22222222-2222-2222-2222-222222222222"
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300" '
        f'viewBox="0 0 400 300">'
        f'<g id="shape-{shape_id}">'
        f'<rect x="10" y="20" width="30" height="40" '
        f'transform="matrix(1,0,0,1,5,5)"/>'
        f"</g></svg>"
    ).encode("utf-8")
    data = S.crop_svg(svg, shape_id)
    root = data.decode("utf-8")
    # (10,20)-(40,60) translated by (5,5) -> (15,25)-(45,65)
    assert 'viewBox="15 25 30 40"' in root


def test_crop_of_an_unknown_shape_is_refused():
    with pytest.raises(S.ShapeNotFound, match="not-a-real-id"):
        S.crop_svg(SAMPLE_SVG, "not-a-real-id")


def test_crop_of_a_shape_with_no_recoverable_geometry_is_refused():
    """A shape built only from a primitive this module does not understand
    (here: a bare <path>, standing in for a curve or text-as-path) must fail
    loudly rather than crop to a fabricated box."""
    shape_id = "33333333-3333-3333-3333-333333333333"
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300" '
        f'viewBox="0 0 400 300">'
        f'<g id="shape-{shape_id}"><path d="M0 0 L10 10 Z"/></g></svg>'
    ).encode("utf-8")
    with pytest.raises(S.SvgPostError, match="no rendered geometry"):
        S.crop_svg(svg, shape_id)


# --- postprocess -------------------------------------------------------

def test_postprocess_applies_swap_then_crop():
    spec = R.RenderSpec(swaps=(("ffffff", "ff0000"),), crop=f"shape-{ROOT_ID}")
    data, problems = S.postprocess(SAMPLE_SVG, spec)
    text = data.decode("utf-8")
    assert "rgb(255, 0, 0)" in text
    assert 'viewBox="0 0 400 300"' in text
    assert problems == []


def test_scale_is_a_no_op_at_one():
    """The default must be byte-identical, so an unscaled render is exactly
    what the exporter produced."""
    data, problems = S.postprocess(SAMPLE_SVG, R.RenderSpec(scale=1.0))
    assert data == SAMPLE_SVG
    assert problems == []


def test_scale_resizes_the_root_but_not_the_viewbox():
    """Penpot ignores `scale` for SVG (verified live: scale=2 came back at the
    board's own dimensions), so this module applies it as the presentation
    size. The viewBox must survive untouched -- scaling it instead would
    change which part of the board is visible rather than how big it is."""
    data, _ = S.postprocess(SAMPLE_SVG, R.RenderSpec(scale=2.0))
    text = data.decode("utf-8")
    assert 'width="800"' in text
    assert 'height="600"' in text
    assert 'viewBox="0 0 400 300"' in text


def test_scale_leaves_inner_shape_geometry_alone():
    """Only the root element's own width/height move. A shape's width= is
    governed by the viewBox and must not be doubled a second time."""
    data, _ = S.postprocess(SAMPLE_SVG, R.RenderSpec(scale=2.0))
    text = data.decode("utf-8")
    body = text.partition(">")[2]
    assert 'width="800"' not in body


def test_scale_applies_after_crop():
    """A crop rewrites width/height to the cropped box; the scale must then
    multiply *that*, not the original board size."""
    spec = R.RenderSpec(scale=3.0, crop=f"shape-{ROOT_ID}")
    data, _ = S.postprocess(SAMPLE_SVG, spec)
    text = data.decode("utf-8")
    assert 'viewBox="0 0 400 300"' in text
    assert 'width="1200"' in text
    assert 'height="900"' in text


# --- inlining ----------------------------------------------------------

EXTERNAL_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
    '<style>@font-face { src: url(http://penpot.test/internal/gfonts/f.woff2); }</style>'
    '<image href="http://penpot.test/assets/by-file-media-id/abc"/>'
    '<image href="http://penpot.test/assets/by-file-media-id/abc"/>'
    '</svg>'
).encode("utf-8")


def test_inlining_replaces_both_reference_shapes():
    """Penpot emits image fills as href= and web fonts as url() inside its own
    <style> block. Both are absolute against Penpot's origin, so both must
    come along or the render is holed for anyone else."""
    def fetch(url):
        return ("font/woff2" if url.endswith(".woff2") else "image/jpeg"), b"xy"

    data, problems = S.inline_externals(EXTERNAL_SVG, fetch)
    text = data.decode("utf-8")
    assert problems == []
    assert "http://penpot.test" not in text
    assert "url(data:font/woff2;base64,eHk=)" in text
    assert 'href="data:image/jpeg;base64,eHk="' in text


def test_each_url_is_fetched_once_however_often_it_appears():
    calls: list[str] = []

    def fetch(url):
        calls.append(url)
        return "image/jpeg", b"xy"

    S.inline_externals(EXTERNAL_SVG, fetch)
    assert len(calls) == len(set(calls)) == 2


def test_an_unfetchable_reference_is_reported_not_dropped():
    """The reference must survive verbatim. Dropping it would leave a render
    that looks complete and is not -- exactly the silent-blank failure this
    service exists to refuse."""
    def fetch(url):
        raise RuntimeError("boom")

    data, problems = S.inline_externals(EXTERNAL_SVG, fetch)
    text = data.decode("utf-8")
    assert 'href="http://penpot.test/assets/by-file-media-id/abc"' in text
    assert "url(http://penpot.test/internal/gfonts/f.woff2)" in text
    assert len(problems) == 2
    assert all("boom" in p for p in problems)


def test_a_document_with_no_external_references_is_byte_identical():
    def fetch(url):  # pragma: no cover -- must never be called
        raise AssertionError("nothing to fetch")

    data, problems = S.inline_externals(SAMPLE_SVG, fetch)
    assert data == SAMPLE_SVG
    assert problems == []


# --- what the second review found ---------------------------------------

def test_scale_survives_an_xml_declaration_before_the_root():
    """Penpot adds an XML declaration when it re-serialises a document on
    import, so a render that has been through the refresh plugin has one.
    Partitioning on the first `>` would hand back the declaration and scale
    nothing, silently."""
    svg = b'<?xml version="1.0"?><svg width="100" height="50" viewBox="0 0 100 50">'
    out = S.scale_svg(svg, 2.0)
    assert b'width="200"' in out
    assert b'height="100"' in out
    assert b'viewBox="0 0 100 50"' in out


def test_scale_does_not_match_a_hyphenated_attribute():
    """`\\b` also matches after a hyphen, so `stroke-width` on the root would
    be scaled as if it were the width -- and then the real height would be
    skipped, changing the aspect ratio."""
    out = S.scale_svg(b'<svg stroke-width="4" width="200" height="50">', 2.0)
    assert b'stroke-width="4"' in out
    assert b'width="400"' in out
    assert b'height="100"' in out


def test_scale_leaves_a_unit_bearing_length_alone():
    out = S.scale_svg(b'<svg width="100%" height="50">', 2.0)
    assert b'width="100%"' in out


def test_scale_never_emits_scientific_notation():
    """`width="1.39e+06"` is not a valid SVG length; the browser renders
    nothing. renderspec clamps the factor, and this keeps the spelling plain
    even so."""
    out = S.scale_svg(b'<svg width="1390" height="724">', 20.0)
    assert b"e+" not in out
    assert b'width="27800"' in out


def test_a_quoted_font_url_is_inlined_too():
    """An unquoted url() is what Penpot emits today. A quoted one that stayed
    external would be the silent fallback-font failure inlining exists to
    fix, and would not even be reported."""
    svg = b'<svg><style>@font-face{src:url("https://p.example/f.woff2")}</style></svg>'
    out, problems = S.inline_externals(svg, lambda url: ("font/woff2", b"AA"))
    assert b"data:font/woff2;base64,QUE=" in out
    assert b"https://" not in out
    assert problems == []


def test_only_an_images_href_is_inlined():
    """A bare href= match would rewrite an `<a href>` into a data:text/html
    link -- neither wanted nor safe."""
    svg = b'<svg><a href="https://ext.example/page">x</a></svg>'
    out, problems = S.inline_externals(svg, lambda url: ("text/html", b"AA"))
    assert out == svg
    assert problems == []


def test_a_content_type_that_breaks_out_of_the_attribute_is_refused_at_the_splice():
    """Defence in depth: the fetcher vets this, but the value lands inside an
    attribute in a document served to every viewer, so it is checked again
    where the splice happens rather than trusted across a callable."""
    svg = b'<svg><image href="https://p.example/a.png"/></svg>'
    out, problems = S.inline_externals(
        svg, lambda url: ('image/png" onload="alert(1)', b"AA"))
    assert out == svg
    assert len(problems) == 1
    assert "not a bare type/subtype" in problems[0]


def test_an_unparseable_paint_does_not_claim_a_swap_was_missed():
    """`currentColor` may denote anything. Reporting it as a definite miss on
    every render that carries one makes the problems channel noise."""
    svg = b'<svg><rect fill="currentColor"/><rect style="fill: rgb(255, 0, 0);"/></svg>'
    data, problems = S.swap_svg(svg, (("ff0000", "00ff00"),))
    assert b"rgb(0, 255, 0)" in data
    assert len(problems) == 1
    assert "cannot parse" in problems[0]
    assert "only partly swapped" not in problems[0]


def test_hex_inside_a_style_declaration_is_reported_as_a_partial_swap():
    """Matched by neither pass, so it comes back unchanged. Unreported, that
    is an unchanged picture alongside a clean report."""
    svg = b'<svg><g style="fill: #ff0000"/></svg>'
    data, problems = S.swap_svg(svg, (("ff0000", "00ff00"),))
    assert data == svg
    assert len(problems) == 1
    assert "only partly swapped" in problems[0]

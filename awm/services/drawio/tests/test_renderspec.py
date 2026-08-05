"""The view URL's parameters: parsing, canonical spelling, and what they touch.

The failures worth pinning here are the quiet ones. A swap that chains instead
of applying simultaneously produces a picture that looks plausible and is wrong.
A blank parameter that vanishes before anyone can complain renders the plain
page and reports success. A substitution that reaches an inlined data URI
corrupts an image with no error anywhere. Each has a test below.
"""

from __future__ import annotations

import pytest

from awm.drawio import renderspec as R
from awm.drawio import xmlmodel

PAGE = """<mxfile>
  <diagram id="p1" name="Page-1">
    <mxGraphModel grid="1" pageWidth="850">
      <root>
        <mxCell id="0" />
        <mxCell id="1" parent="0" />
        <mxCell id="a" value="A" style="rounded=0;fillColor=#FF00FF;strokeColor=#f0f;"
                vertex="1" parent="1">
          <mxGeometry x="0" y="0" width="80" height="40" as="geometry" />
        </mxCell>
        <mxCell id="frame" value="frame-a" style="rounded=0;fillColor=none;"
                vertex="1" parent="1">
          <mxGeometry x="10" y="10" width="200" height="100" as="geometry" />
        </mxCell>
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
"""


# --- canonical spelling ----------------------------------------------------

@pytest.mark.parametrize("token", ["ff00ff", "#ff00ff", "%23FF00FF", "f0f",
                                   "#F0F", " ff00ff "])
def test_every_spelling_of_one_colour_canonicalises(token):
    assert R.canonical_colour(token) == "ff00ff"


def test_eight_digit_rgba_is_refused_not_truncated():
    """Accepting it would make the width rule ambiguous; silently dropping the
    alpha would change the picture."""
    with pytest.raises(R.SpecError, match="eight-digit"):
        R.canonical_colour("ff00ff80")


@pytest.mark.parametrize("token", ["red", "rgb(1,2,3)", "gg0000", "ff00f", ""])
def test_junk_colour_names_the_token(token):
    with pytest.raises(R.SpecError) as exc:
        R.canonical_colour(token)
    assert repr(token.strip()) in str(exc.value) or "not a 3- or 6-digit" in str(exc.value)


def test_swap_without_a_colon_is_refused():
    with pytest.raises(R.SpecError, match="ff00ff00aa55"):
        R.parse_swap("ff00ff00aa55")


def test_one_source_may_not_be_swapped_twice():
    with pytest.raises(R.SpecError, match="swapped twice"):
        R.parse_swaps(["ff00ff:00aa55", "f0f:112233"])


# --- the query round trip --------------------------------------------------

def test_parameter_order_does_not_change_the_spec_or_its_fingerprint():
    a = R.from_query({"swap": ["ff00ff:00aa55", "00ff00:333333"]})
    b = R.from_query({"swap": ["00ff00:333333", "ff00ff:00aa55"]})
    assert a == b
    assert R.to_query(a) == R.to_query(b)
    assert R.fingerprint(a) == R.fingerprint(b)


def test_a_blank_swap_is_an_error_not_a_shrug():
    """`parse_qs` drops blank values by default, which would render the plain
    page and call it success — the handler keeps them precisely so this fires."""
    with pytest.raises(R.SpecError, match="empty swap"):
        R.from_query({"swap": [""]})


def test_a_blank_crop_is_an_error():
    with pytest.raises(R.SpecError, match="empty crop"):
        R.from_query({"crop": [""]})


def test_unknown_parameters_are_ignored():
    """The consumer client appends a cache-buster and `rev` selects a revision;
    neither describes a render, so neither may perturb the variant."""
    spec = R.from_query({"bust": ["1758"], "rev": ["abc123"]})
    assert spec.is_plain and R.fingerprint(spec) == "__plain__"


def test_a_junk_scale_still_renders():
    """Scale keeps its tolerant fallback: a bad one is a size, not a meaning."""
    assert R.from_query({"scale": ["wat"]}).scale == 1.0


def test_the_plain_spec_keeps_the_original_cache_identity():
    assert R.fingerprint(R.DEFAULT) == "__plain__"
    assert R.to_query(R.DEFAULT) == ""


def test_the_query_is_readable():
    spec = R.from_query({"swap": ["ff00ff:00aa55"], "crop": ["frame-a"]})
    assert R.to_query(spec) == "swap=ff00ff:00aa55&crop=frame-a"


# --- substitution ----------------------------------------------------------

def test_swaps_are_simultaneous_never_chained():
    """The failure this prevents: aa0000 arriving at cc0000 via bb0000."""
    swaps = R.parse_swaps(["aa0000:bb0000", "bb0000:cc0000"])
    text, hits = R.swap_text("#aa0000 #bb0000", swaps)
    assert text == "#bb0000 #cc0000"
    assert hits == 2


def test_the_short_spelling_of_a_colour_is_swapped_too():
    swaps = R.parse_swaps(["ff00ff:00aa55"])
    assert R.swap_text("#f0f", swaps)[0] == "#00aa55"
    assert R.swap_text("#FF00FF", swaps)[0] == "#00aa55"


def test_a_six_digit_match_does_not_eat_an_eight_digit_value():
    swaps = R.parse_swaps(["ff00ff:00aa55"])
    assert R.swap_text("#ff00ff80", swaps) == ("#ff00ff80", 0)


def test_a_three_digit_source_declines_a_longer_colour():
    swaps = R.parse_swaps(["aabbcc:112233"])
    assert R.swap_text("#abcdef", swaps) == ("#abcdef", 0)
    assert R.swap_text("#abc", swaps)[0] == "#112233"


def test_a_swap_that_matches_nothing_is_not_an_error():
    assert R.swap_text("#123456", R.parse_swaps(["ff00ff:00aa55"])) == \
        ("#123456", 0)


# --- document-level substitution -------------------------------------------

def test_style_colours_and_labels_are_swapped():
    swaps = R.parse_swaps(["ff00ff:00aa55"])
    out, hits = R.swap_document(PAGE, swaps)
    assert hits == 2                      # fillColor and strokeColor
    assert "#00aa55" in out and "ff00ff" not in out.lower()


def test_an_inlined_data_uri_is_never_touched():
    """The whole reason substitution is key-scoped rather than a text replace:
    an `image=` value can be a megabyte of payload that must come through
    byte-identical."""
    payload = "data:image/svg+xml,%3Csvg%20fill%3D%22%23ff00ff%22%2F%3Eff00ff"
    xml = PAGE.replace('style="rounded=0;fillColor=#FF00FF;strokeColor=#f0f;"',
                       f'style="shape=image;image={payload};fillColor=#ff00ff;"')
    out, _ = R.swap_document(xml, R.parse_swaps(["ff00ff:00aa55"]))
    assert payload in out
    assert "fillColor=#00aa55" in out


def test_no_substitution_introduces_a_semicolon():
    out, _ = R.swap_document(PAGE, R.parse_swaps(["ff00ff:00aa55"]))
    style = out.split('style="')[1].split('"')[0]
    assert style.count(";") == PAGE.split('style="')[1].split('"')[0].count(";")


def test_an_html_label_is_swapped():
    xml = PAGE.replace('value="A"',
                       'value="&lt;font color=&quot;#ff00ff&quot;&gt;x&lt;/font&gt;"')
    out, _ = R.swap_document(xml, R.parse_swaps(["ff00ff:00aa55"]))
    assert "#00aa55" in out


def test_no_swaps_leaves_the_document_untouched():
    assert R.swap_document(PAGE, ()) == (PAGE, 0)


def test_a_compressed_diagram_fails_loudly():
    """It renders fine but cannot be rewritten, and an un-swapped render is
    indistinguishable from a swap that matched nothing."""
    compressed = '<mxfile><diagram id="p" name="P">7Vtbc9o4</diagram></mxfile>'
    with pytest.raises(xmlmodel.CompressedDiagram):
        R.swap_document(compressed, R.parse_swaps(["ff00ff:00aa55"]))


# --- crop ------------------------------------------------------------------

def test_crop_resolves_a_label_to_a_cell_id():
    out, cell_id = R.prepare_crop(PAGE, 0, "frame-a")
    assert cell_id == "frame"
    assert R.CROP_FRAME_STYLE in out


def test_crop_resolves_a_cell_id_too():
    _, cell_id = R.prepare_crop(PAGE, 0, "frame")
    assert cell_id == "frame"


def test_the_frame_is_made_paintable_and_unlabelled():
    """drawio builds no state for an invisible cell, so a hidden frame is one
    the browser cannot measure; the label would otherwise print inside it."""
    out, _ = R.prepare_crop(PAGE, 0, "frame-a")
    frame = out.split('id="frame"')[1].split("/>")[0]
    assert "strokeColor=#000000" in frame
    assert 'value=""' in frame or "value=" not in frame


def test_an_unknown_crop_name_says_so():
    with pytest.raises(R.CropNotFound, match="ghost"):
        R.prepare_crop(PAGE, 0, "ghost")

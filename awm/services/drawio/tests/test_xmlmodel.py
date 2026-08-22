"""Canonical serialization: the property the merge story rests on.

If browser output and script output cannot be made to converge on one spelling,
git's line merge sees phantom conflicts everywhere and the concurrency contract
degenerates to whole-file replacement. These tests pin that convergence, and
pin the things normalization must *not* do — reordering siblings would silently
change z-order, which is the exact class of failure the design exists to avoid.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from awm.drawio import xmlmodel as X

MINIMAL = """<mxfile host="Electron" agent="Chrome/1.0" version="29.6.6" pages="1">
  <diagram id="p1" name="Page-1">
    <mxGraphModel dx="1097" dy="698" grid="1" pageWidth="850">
      <root>
        <mxCell id="0" />
        <mxCell id="1" parent="0" />
        <mxCell id="a" value="A" style="rounded=1;" vertex="1" parent="1">
          <mxGeometry x="10" y="20" width="80" height="40" as="geometry" />
        </mxCell>
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>
"""


def test_normalize_is_idempotent():
    once = X.normalize(MINIMAL)
    assert X.normalize(once) == once


def test_viewport_churn_produces_no_diff():
    scrolled = MINIMAL.replace('dx="1097" dy="698"', 'dx="4200" dy="99"')
    assert X.normalize(scrolled) == X.normalize(MINIMAL)


def test_editor_identity_churn_produces_no_diff():
    other_browser = MINIMAL.replace(
        'host="Electron" agent="Chrome/1.0" version="29.6.6"',
        'host="127.0.0.1" agent="Firefox/2.0" version="30.0.0"',
    )
    assert X.normalize(other_browser) == X.normalize(MINIMAL)


def test_attribute_order_does_not_matter():
    swapped = MINIMAL.replace('id="p1" name="Page-1"', 'name="Page-1" id="p1"')
    assert X.normalize(swapped) == X.normalize(MINIMAL)


def test_float_noise_is_rounded_away():
    noisy = MINIMAL.replace('x="10"', 'x="9.999999999999998"')
    assert X.normalize(noisy) == X.normalize(MINIMAL)


def test_non_geometry_attributes_are_never_rewritten():
    """A cell id or label that happens to parse as a float must survive intact."""
    doc = MINIMAL.replace('value="A"', 'value="1.500000000000001"')
    assert 'value="1.500000000000001"' in X.normalize(doc)


def test_page_count_is_recomputed_not_trusted():
    stale = MINIMAL.replace('pages="1"', 'pages="7"')
    assert 'pages="1"' in X.normalize(stale)


def test_sibling_order_is_preserved():
    """Sibling order is z-order; sorting it would be a silent render change."""
    doc = MINIMAL.replace(
        '<mxCell id="a"', '<mxCell id="z" vertex="1" parent="1" /><mxCell id="a"'
    )
    out = X.normalize(doc)
    assert out.index('id="z"') < out.index('id="a"')


def test_waypoint_order_is_preserved():
    doc = """<mxfile><diagram id="p"><mxGraphModel><root>
      <mxCell id="0" /><mxCell id="1" parent="0" />
      <mxCell id="e" edge="1" parent="1"><mxGeometry as="geometry">
        <Array as="points">
          <mxPoint x="300" y="10" /><mxPoint x="100" y="20" />
        </Array></mxGeometry></mxCell>
    </root></mxGraphModel></diagram></mxfile>"""
    out = X.normalize(doc)
    assert out.index('x="300"') < out.index('x="100"')


def test_compressed_diagram_is_refused():
    doc = ('<mxfile><diagram id="p" name="P">7VpbU9s4FP41ecz'
           'GXhLI4y6l7WydTic7290nRrEVW1SWjCwnhF+/kiXf5EBoS'
           '2E7fUh9jqSjc/nOJXJG8DzbveGoSD+wGNORN4t3I/h65HmA'
           'BF75RyH3DTL3vAZIOIn1oh64Ig9YgzONliTGxWChYIwKUgz'
           'BiDGGIzHAEOdsO1y2ZnRotEAJ3gOuIkT30b9JLNIGPfPPO/'
           'wtJknaWgYzPZOhdrEGihTFbNuD4MUInnPGRPOU7c4xVdi1u'
           'DTvvXlgtjsYx1w85wX6AaVfSlqNfPk2vfxntv0dj71GygbR'
           'Ujusjyv2LQZKuMBcpcxrn5AlZLxRZ+eShGPMxIzWMk08J3Q'
           'bfaW0KKm3tORHhCU4KV3XKMkY+t5/CIgxDLVGFOWJVvcv+U'
           'BKtE0KsRW1lVFhTDlIYPXfSKV7wYqjM9lMEG65mbF1Uy19P'
           'ZzZP0LKikkD9xnbBZI/uRoo/2G81nnCXqI9NuI4PJf5wbeI'
           'jkiPMc/GpQvY3rNL3xDlDsSU6Z6xxlmwPMdlz+cGyOgD8Xj'
           'JR3f6RQ+DEODcCcnPnhcezcOTMEBTeUcJyKUJWIkRAo9+bA'
           '85UfHiHhePkXjPjCngZUvhHiC5FeecCrN9gHZlzUUpFbOTn'
           'GsRWcZUFERGDDNSRFEXbAtHNCPPQFbfPOtRnZ1U+MdvOTHl'
           'ffOLevfQXWU5N5Wnyu36wGvKPEjOKXwfx3nMx9BFdvBSAsN'
           'PsSAsRTiQu5Cty+d47zJUdtgUcO7t6R6l4bGeqXQPpcNPvv'
           'X4Wr2jaBc8Ck7Zk1BLB86jmm3gYYK+xzE6TE0IX1BSpAgWG'
           'CFqoqmzYWlUZI0oSSeqSFV+RyXwsMwQxvcRfKMzXPQxNKvV'
           'RzHVSAgphsRc1vDcHmwmXmpBQTv4ipXKmKzWJEbLhCkOnzO'
           'YbySF9NDDBnR9BbP4WEBS9CQI0DAgKXpiQAJnR8kFN18AAF'
           'g+8Cb2/PLDPJ6dTOd+cO1Nrq/9ycn5xdRfLGb+ZDafn12dz'
           '8Kri/mFpsSvLmSAGsMzsBRuBGrpQKQaFAWHmwsu1qeHi1//'
           'PDMYnRUaR2eIC0EFH+lZW76a4POM0BhtHZ44hebWJlWU5Wg'
           'lD46KFHF0FZBtxzYkTh1FKh0hh8+CxIJUUpcgjhKO8u3PLo'
           'Ce46kv1SXVCbUnruy2W7+kAe0RfsC8xdDNRrNbAX1yELnyk'
           'i6mDLLLXcaRfsFDbSlm7yA7SXlZjEZ2mCoO9k4nvvftGFXj'
           'iBtOJyrYPnJKgL78bC5rgHo2A/CIf0uMxi0BpyU+HGPXFbz'
           'HHwLdIrmtGaGXY9OD8lXqM17MEUOZBLCVGqM9GoQeMy9k47'
           'wnfDA+dP4ldnBl0uL08uzsPzQVdtfvIcPLzPHTDXKSCdgQm'
           'zUsdyGZ3wPQK8xnQ5tvxCngD+8kAy4B0FQAAA==</diagram></mxfile>')
    with pytest.raises(X.CompressedDiagram):
        X.parse(doc)


def test_non_mxfile_is_refused():
    with pytest.raises(X.MalformedDiagram):
        X.parse("<html><body>nope</body></html>")


def test_malformed_xml_is_refused():
    with pytest.raises(X.MalformedDiagram):
        X.parse("<mxfile><diagram>")


def test_normalization_is_content_preserving():
    """Every cell, attribute and parent/child relation survives unchanged.

    Stronger than eyeballing the output: normalization only ever drops the
    attributes it explicitly claims to drop, and never touches structure.
    """
    before = ET.fromstring(MINIMAL)
    after = ET.fromstring(X.normalize(MINIMAL))

    def skeleton(elem, drop=frozenset()):
        attrs = {k: v for k, v in elem.attrib.items() if k not in drop}
        if elem.tag in X.COORD_ELEMENTS:
            attrs = {k: (X._canon_number(v) if k in X.COORD_ATTRS else v)
                     for k, v in attrs.items()}
        return (elem.tag, attrs, [skeleton(c, drop) for c in elem])

    dropped = X.MXFILE_VOLATILE | X.VIEWPORT_ATTRS | {"pages"}
    assert skeleton(before, dropped) == skeleton(after, dropped)


def test_page_summaries_excludes_structural_cells():
    pages = X.page_summaries(X.parse(MINIMAL))
    assert pages == [{"id": "p1", "name": "Page-1", "cells": 1}]


def test_special_characters_round_trip():
    doc = MINIMAL.replace('value="A"', 'value="a &amp; b &lt;x&gt; &quot;q&quot;"')
    once = X.normalize(doc)
    assert X.normalize(once) == once
    assert ET.fromstring(once).find(".//mxCell[@id='a']").get("value") == \
        'a & b <x> "q"'


# --- cutting a document down to one page -----------------------------------

TWO = MINIMAL.replace("</mxfile>", """  <diagram id="p2" name="Page-2">
    <mxGraphModel grid="1" pageWidth="850">
      <root>
        <mxCell id="0" />
        <mxCell id="1" parent="0" />
        <mxCell id="b" value="B" vertex="1" parent="1" />
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>""")


def test_a_cut_page_is_byte_identical_to_the_page_it_came_from():
    """The view cache keys on the serialized page, so a cut that perturbed a
    single byte would orphan every render already in the store."""
    inside = X.serialize(X.parse(TWO).findall("diagram")[1])
    alone = X.serialize(X.parse(X.single_page(TWO, 1)).findall("diagram")[0])
    assert alone == inside


def test_a_cut_keeps_the_documents_own_attributes():
    cut = X.parse(X.single_page(TWO, 1))
    assert [d.get("name") for d in cut.findall("diagram")] == ["Page-2"]
    assert cut.get("pages") == "1"


def test_cutting_past_the_end_is_refused():
    with pytest.raises(X.MalformedDiagram, match="out of range"):
        X.single_page(TWO, 5)


def test_the_tree_handed_in_is_left_alone():
    """Callers pass a tree they parsed for something else — resolving a page
    name, most often — and go on using it."""
    tree = X.parse(TWO)
    X.single_page(tree, 0)
    assert len(tree.findall("diagram")) == 2

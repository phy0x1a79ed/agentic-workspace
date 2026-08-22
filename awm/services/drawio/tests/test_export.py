"""Export: self-containment, and the semicolon landmine that shaped it.

The whole reason images are filesystem references rather than embedded data is
that a reference stays live when the image is re-rendered. That only works if
export can still produce something self-contained — otherwise every published
figure silently depends on a running gateway.

These tests pin the inlining rules without needing docker; the render path
itself is verified live (see INSTALL.md § Verify).
"""

from __future__ import annotations

import io
from urllib.parse import unquote_to_bytes

import pytest

from awm.drawio import export
from awm.drawio.dwg import parse_style


@pytest.fixture()
def svg(tmp_path):
    # Deliberately contains a ';' inside a CSS block — routine in real SVG, and
    # exactly what breaks a naive data URI.
    path = tmp_path / "molecule.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        '<style>.r{fill:#e0115f;stroke:#000}</style>'
        '<circle class="r" cx="5" cy="5" r="4"/></svg>',
        encoding="utf-8",
    )
    return path


def test_data_uri_is_never_base64(svg):
    """`data:image/svg+xml;base64,…` truncates at the ';' when drawio parses
    the style, and the cell renders blank with nothing logged."""
    uri = export.data_uri(svg)
    assert uri.startswith("data:image/svg+xml,")
    assert ";base64" not in uri


def test_data_uri_escapes_semicolons_in_the_payload(svg):
    """Not just the header: a ';' inside the SVG's own CSS would truncate too."""
    uri = export.data_uri(svg)
    assert ";" not in uri


def test_inlined_style_still_parses_as_one_segment(svg):
    """The real check — round-trip the rewritten style through drawio's own
    splitting rule and confirm the image survives whole."""
    xml = f'<mxCell style="shape=image;image=/files{svg};" />'
    inlined, problems = export.inline_images(xml)
    assert not problems

    style = inlined.split('style="')[1].split('"')[0]
    parsed = dict(seg for seg in parse_style(style) if seg[1] is not None)
    assert parsed["image"].startswith("data:image/svg+xml,")
    assert "circle" in parsed["image"] or "%3Ccircle" in parsed["image"]


def test_binary_images_inline_without_a_semicolon(tmp_path):
    png = tmp_path / "figure.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)))
    uri = export.data_uri(png)
    assert uri.startswith("data:image/png,") and ";" not in uri


def test_missing_reference_is_reported_not_swallowed(tmp_path):
    xml = '<mxCell style="image=/files/nowhere/ghost.svg;" />'
    inlined, problems = export.inline_images(xml)
    assert len(problems) == 1 and "ghost.svg" in problems[0]
    # Left untouched, so the caller can still see what was meant.
    assert "/files/nowhere/ghost.svg" in inlined


def test_oversized_image_is_refused(tmp_path, monkeypatch):
    big = tmp_path / "huge.png"
    big.write_bytes(b"\x00" * 2048)
    monkeypatch.setattr(export, "MAX_INLINE_BYTES", 1024)
    with pytest.raises(export.ExportError, match="inline limit"):
        export.data_uri(big)


def test_unknown_format_is_refused():
    with pytest.raises(export.ExportError, match="unknown format"):
        export.render("<mxfile/>", "docx")


# --- placed page views: the other half of self-containment -----------------
#
# A diagram can embed another diagram's page as a live SVG at
# `/drawio-app/view/…`. That URL is origin-relative, so a document that keeps
# one is a picture that only works on this host — and until the exporter learned
# to resolve them, nothing said so: the problems list and `check` both looked
# only for `/files`, so the gate that refuses a broken export passed cleanly.

VIEW = "/drawio-app/view"


def echo_resolver(seen=None):
    """Stands in for the renderer. Echoes the URL so a test can prove which
    reference produced which embedded image."""
    def resolve(url, budget):
        if seen is not None:
            seen.append(url)
        return f"<svg><title>{url}</title></svg>".encode("utf-8"), []
    return resolve


def test_a_placed_page_view_is_embedded_as_data(tmp_path):
    xml = f'<mxCell style="shape=image;image={VIEW}/fig/a.drawio/Page-1;" />'
    inlined, problems = export.inline_images(xml, resolver=echo_resolver())
    assert not problems
    assert VIEW not in inlined
    assert "data:image/svg+xml," in inlined


def test_an_exported_document_reaches_back_to_nothing(tmp_path):
    """The assertion nothing made before this: sweep the output for anything
    that would need a gateway to display.

    Namespace declarations are excluded deliberately — every SVG carries
    `xmlns="http://www.w3.org/2000/svg"`, and it is a name, not an address.
    Checking raw `http://` would either fail here or, worse, pass only because
    the fixture happened to have no namespace.
    """
    import re

    image = tmp_path / "m.svg"
    image.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    xml = (f'<mxCell style="image=/files{image};" />'
           f'<mxCell style="image=http://127.0.0.1:7819{VIEW}/fig/a.drawio;" />')
    inlined, problems = export.inline_images(xml, resolver=echo_resolver())
    assert not problems
    assert VIEW not in inlined and "/files" not in inlined

    fetchable = [u for u in re.findall(r"https?://[^\s\"'<>)]+", inlined)
                 if not u.startswith("http://www.w3.org/")]
    assert fetchable == []


def test_an_escaped_ampersand_query_survives_whole():
    """In stored XML a style attribute is escaped, so a multi-parameter query
    reads `&amp;`. A terminator set containing ';' cuts the URL at '&amp' and
    silently drops every parameter but the first — with repeated `swap` that is
    the common case, not an edge."""
    seen = []
    url = f"{VIEW}/fig/a.drawio/Page-1?swap=ff00ff:00aa55&amp;swap=00ff00:333333"
    xml = f'<mxCell style="shape=image;image={url};" />'
    inlined, problems = export.inline_images(xml, resolver=echo_resolver(seen))
    assert not problems
    assert seen == [f"{VIEW}/fig/a.drawio/Page-1"
                    "?swap=ff00ff:00aa55&swap=00ff00:333333"]
    assert "&amp" not in inlined


def test_two_colours_of_one_page_embed_as_two_images():
    xml = (f'<mxCell style="image={VIEW}/fig/a.drawio/P?swap=ff00ff:00aa55;" />'
           f'<mxCell style="image={VIEW}/fig/a.drawio/P?swap=ff00ff:0055aa;" />')
    inlined, _ = export.inline_images(xml, resolver=echo_resolver())
    uris = [seg.split(";")[0] for seg in inlined.split("image=")[1:]]
    assert len(uris) == 2 and uris[0] != uris[1]


def test_a_view_url_containing_files_is_not_mangled():
    """One alternation, not two passes: a diagram stored under a path with
    `files` in it yields a view URL with `/files/…` inside, and a separate
    `/files` pass would rewrite the middle of the URL."""
    seen = []
    url = f"{VIEW}/files/report.drawio/Page-1"
    xml = f'<mxCell style="image={url};" />'
    inlined, problems = export.inline_images(xml, resolver=echo_resolver(seen))
    assert not problems and seen == [url]
    assert "data:image/svg+xml," in inlined


def test_an_unresolvable_page_view_is_reported_not_swallowed():
    def fail(url, budget):
        raise export.ExportError("no page named 'Page-9'")

    xml = f'<mxCell style="image={VIEW}/fig/a.drawio/Page-9;" />'
    inlined, problems = export.inline_images(xml, resolver=fail)
    assert len(problems) == 1 and "Page-9" in problems[0]
    assert VIEW in inlined      # left in place so the intent is still visible


def test_a_problem_one_level_down_stays_visible_at_the_top():
    def nested(url, budget):
        return b"<svg/>", ["/files/ghost.png: missing"]

    xml = f'<mxCell style="image={VIEW}/fig/a.drawio/P;" />'
    _, problems = export.inline_images(xml, resolver=nested)
    assert problems == [f"{VIEW}/fig/a.drawio/P -> /files/ghost.png: missing"]


def test_the_fan_out_budget_is_reported_not_crashed():
    budget = export.Budget(max_renders=2)

    def resolve(url, b):
        b.spend_render()
        return b"<svg/>", []

    xml = "".join(f'<mxCell style="image={VIEW}/fig/a.drawio/P{i};" />'
                  for i in range(4))
    _, problems = export.inline_images(xml, resolver=resolve, budget=budget)
    assert len(problems) == 2 and "embedded page views" in problems[-1]


def test_the_byte_budget_is_reported_not_an_out_of_memory():
    budget = export.Budget(max_bytes=100)
    xml = "".join(f'<mxCell style="image={VIEW}/fig/a.drawio/P{i};" />'
                  for i in range(3))
    _, problems = export.inline_images(
        xml, resolver=lambda url, b: (b"x" * 80, []), budget=budget)
    assert problems and "budget" in problems[-1]


def test_swaps_reach_a_referenced_svg_before_it_is_encoded(tmp_path):
    """After encoding a '#ff00ff' has become '%23ff00ff'; a later pass would
    have to match encoded spellings and risk splicing a ';' back in."""
    from awm.drawio import renderspec

    image = tmp_path / "mask.svg"
    image.write_text('<svg xmlns="http://www.w3.org/2000/svg">'
                     '<rect fill="#ff00ff"/></svg>', encoding="utf-8")
    swaps = renderspec.parse_swaps(["ff00ff:00aa55"])
    uri = export.data_uri(image, swaps=swaps)
    assert "%2300aa55" in uri and "%23ff00ff" not in uri
    assert ";" not in uri


def test_a_raster_image_is_recoloured_too(tmp_path):
    """A masked region inside a PNG is reachable — the pixels are decoded, the
    colour pass runs, and the file keeps its own format on the way back out."""
    from PIL import Image

    from awm.drawio import renderspec

    png = tmp_path / "figure.png"
    Image.new("RGB", (4, 4), (255, 0, 255)).save(png)
    uri = export.data_uri(png, swaps=renderspec.parse_swaps(["ff00ff:00aa55"]))
    assert uri.startswith("data:image/png,") and ";" not in uri

    decoded = Image.open(io.BytesIO(unquote_to_bytes(uri.split(",", 1)[1])))
    assert decoded.format == "PNG"
    assert decoded.convert("RGB").getpixel((0, 0)) == (0, 0xAA, 0x55)


def test_a_raster_that_matches_nothing_is_byte_identical(tmp_path):
    """The content key is a hash of the inlined document; re-encoding an image
    that did not match would rewrite every cache entry in the store."""
    from PIL import Image

    from awm.drawio import renderspec

    png = tmp_path / "figure.png"
    Image.new("RGB", (4, 4), (18, 52, 86)).save(png)
    before = export.data_uri(png)
    after = export.data_uri(png, swaps=renderspec.parse_swaps(["ff00ff:00aa55"]))
    assert before == after


def test_an_undecodable_image_is_reported_not_silently_skipped(tmp_path):
    """A swap that could not be *attempted* is a problem; one that matched
    nothing is not."""
    from awm.drawio import renderspec

    png = tmp_path / "broken.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n\xff\x00\xff")
    problems: list[str] = []
    export.data_uri(png, swaps=renderspec.parse_swaps(["ff00ff:00aa55"]),
                    problems=problems)
    assert problems and "broken.png" in problems[0]


def test_crop_is_refused_for_a_container_format():
    with pytest.raises(export.ExportError, match="only the SVG path"):
        export.render("<mxfile/>", "png", crop_id="frame")


def test_multiple_references_all_inline(tmp_path):
    paths = []
    for i in range(3):
        p = tmp_path / f"m{i}.svg"
        p.write_text(f'<svg xmlns="http://www.w3.org/2000/svg"><text>{i}</text></svg>',
                     encoding="utf-8")
        paths.append(p)
    xml = "".join(f'<mxCell style="image=/files{p};" />' for p in paths)
    inlined, problems = export.inline_images(xml)
    assert not problems
    assert inlined.count("data:image/svg+xml,") == 3
    assert "/files" not in inlined

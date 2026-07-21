"""Export: self-containment, and the semicolon landmine that shaped it.

The whole reason images are filesystem references rather than embedded data is
that a reference stays live when the image is re-rendered. That only works if
export can still produce something self-contained — otherwise every published
figure silently depends on a running gateway.

These tests pin the inlining rules without needing docker; the render path
itself is verified live (see INSTALL.md § Verify).
"""

from __future__ import annotations

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

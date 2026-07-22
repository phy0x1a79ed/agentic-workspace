"""Native SVG export: the browser-backed path and how `render` routes to it.

The containerized export server cannot produce SVG — it answers "400 Unsupported
Format!" — so SVG goes to drawio's own client in headless Chrome instead. The
routing is what has teeth here: an SVG request that quietly reached the
container would have failed on every diagram, forever, which is exactly the bug
this path exists to fix.

The one test that needs a real browser skips when there isn't one, so this file
is useful on a host with no Chrome and honest about what it did not check.
"""

from __future__ import annotations

import os

import pytest

from awm.drawio import chrome, export

XML = (
    '<mxfile><diagram id="p1" name="Page-1"><mxGraphModel grid="1" '
    'pageWidth="850" pageHeight="1100"><root><mxCell id="0" />'
    '<mxCell id="1" parent="0" />'
    '<mxCell id="a" value="HELLO" vertex="1" parent="1" style="rounded=0;">'
    '<mxGeometry x="40" y="40" width="120" height="60" as="geometry" />'
    '</mxCell></root></mxGraphModel></diagram></mxfile>'
)


# --- routing ---------------------------------------------------------------

def test_svg_never_reaches_the_container(monkeypatch):
    """The container 400s on svg, so a request that got there is a bug."""
    def explode(*args, **kwargs):
        raise AssertionError("svg must not go through the export container")

    monkeypatch.setattr(export, "ensure_container", explode)
    monkeypatch.setattr(chrome, "render_svg",
                        lambda xml, **kw: "<svg>rendered</svg>")

    data, problems = export.render(XML, "svg")
    assert data == b"<svg>rendered</svg>" and problems == []


def test_other_formats_still_go_to_the_container(monkeypatch):
    monkeypatch.setattr(chrome, "render_svg", lambda *a, **k: pytest.fail(
        "png must not go to the browser"))
    monkeypatch.setattr(export, "ensure_container", lambda: "running")

    class Response:
        content = b"%PDF-fake"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(export.httpx, "post", lambda *a, **k: Response())
    data, _ = export.render(XML, "pdf")
    assert data == b"%PDF-fake"


def test_scale_and_page_reach_the_renderer(monkeypatch):
    seen = {}

    def capture(xml, **kwargs):
        seen.update(kwargs)
        return "<svg/>"

    monkeypatch.setattr(chrome, "render_svg", capture)
    export.render(XML, "svg", scale=2.0, page=3)
    assert seen == {"scale": 2.0, "page": 3}


def test_a_browser_failure_surfaces_as_an_export_error(monkeypatch):
    """Callers branch on ExportError; a raw ChromeError would escape them."""
    def fail(*a, **k):
        raise chrome.ChromeError("no Chrome/Chromium on PATH")

    monkeypatch.setattr(chrome, "render_svg", fail)
    with pytest.raises(export.ExportError, match="no Chrome"):
        export.render(XML, "svg")


def test_images_are_inlined_before_the_browser_sees_them(tmp_path, monkeypatch):
    """The page must render with no network reach — inlining is what buys that."""
    image = tmp_path / "m.svg"
    image.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    seen = {}

    def capture(xml, **kwargs):
        seen["xml"] = xml
        return "<svg/>"

    monkeypatch.setattr(chrome, "render_svg", capture)
    export.render(f'<mxCell style="image=/files{image};" />', "svg")
    assert "data:image/svg+xml," in seen["xml"]
    assert f"/files{image}" not in seen["xml"]


# --- configuration ---------------------------------------------------------

def test_export_page_comes_from_the_gateway_mount(monkeypatch):
    monkeypatch.delenv("DRAWIO_EXPORT_PAGE", raising=False)
    monkeypatch.setenv("AWM_HUB_URL", "http://127.0.0.1:7819/")
    assert chrome.export_page_url().endswith("/drawio-app/export3.html")


def test_export_page_can_be_overridden(monkeypatch):
    monkeypatch.setenv("DRAWIO_EXPORT_PAGE", "http://elsewhere/export3.html")
    assert chrome.export_page_url() == "http://elsewhere/export3.html"


def test_no_hub_url_says_so_plainly(monkeypatch):
    monkeypatch.delenv("DRAWIO_EXPORT_PAGE", raising=False)
    monkeypatch.delenv("AWM_HUB_URL", raising=False)
    with pytest.raises(chrome.ChromeError, match="AWM_HUB_URL"):
        chrome.export_page_url()


def test_a_bogus_chrome_override_is_not_used(monkeypatch):
    monkeypatch.setenv("DRAWIO_CHROME", "/nonexistent/chrome")
    assert chrome.chrome_binary() is None


def test_state_without_a_browser(monkeypatch):
    monkeypatch.setattr(chrome, "chrome_binary", lambda: None)
    assert chrome.Browser().state() == "no-chrome"


def test_state_before_first_use(monkeypatch):
    monkeypatch.setattr(chrome, "chrome_binary", lambda: "/usr/bin/chrome")
    assert chrome.Browser().state() == "stopped"


# --- the real thing --------------------------------------------------------

def _browser_available() -> bool:
    if chrome.chrome_binary() is None:
        return False
    try:
        import httpx

        url = chrome.export_page_url()
    except Exception:  # noqa: BLE001
        return False
    try:
        return httpx.get(url, timeout=3).status_code == 200
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _browser_available(),
                    reason="needs Chrome and the gateway's /drawio-app mount")
def test_native_svg_is_really_drawios_own():
    """Not any SVG — drawio's. Live text and a viewBox cropped to the drawing.

    Rasterizing or tracing a PDF would also yield "an SVG"; both would fail
    these assertions, which is the point of making them.
    """
    svg = chrome.render_svg(XML)
    try:
        assert svg.lstrip().startswith("<?xml")
        assert "<text" in svg          # real text, not glyph outlines
        assert "HELLO" in svg          # ...carrying the label
        assert "data-cell-id" in svg   # drawio's own cell annotations
        # Cropped to the shape (120x60 plus a little), not the 850x1100 page.
        box = svg.split('viewBox="')[1].split('"')[0].split()
        assert float(box[2]) < 200 and float(box[3]) < 120
    finally:
        chrome.BROWSER.close()


@pytest.mark.skipif(not _browser_available(),
                    reason="needs Chrome and the gateway's /drawio-app mount")
def test_pages_render_independently():
    """Per-page links are the whole multi-page story; leakage would break them."""
    two = XML.replace(
        "</mxfile>",
        '<diagram id="p2" name="Second"><mxGraphModel><root>'
        '<mxCell id="0" /><mxCell id="1" parent="0" />'
        '<mxCell id="z" value="ELSEWHERE" vertex="1" parent="1">'
        '<mxGeometry x="0" y="0" width="80" height="40" as="geometry" />'
        '</mxCell></root></mxGraphModel></diagram></mxfile>')
    try:
        first = chrome.render_svg(two, page=0)
        second = chrome.render_svg(two, page=1)
        assert "HELLO" in first and "ELSEWHERE" not in first
        assert "ELSEWHERE" in second and "HELLO" not in second
    finally:
        chrome.BROWSER.close()

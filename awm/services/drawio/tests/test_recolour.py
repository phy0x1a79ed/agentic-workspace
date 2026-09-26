"""Applying a colour: to text, to an encoded payload, and to pixels.

The failures pinned here are the ones that leave a picture looking plausible.
A payload decoded with the wrong codec comes back as noise. A payload
re-encoded when nothing matched rewrites every cache key in the store. A
``;base64,`` marker written back into a style string blanks the cell. A raster
mask filled flat instead of shifted turns a shaded region into a blob. Each has
a test below.
"""

from __future__ import annotations

import base64
import io
from urllib.parse import quote

import pytest

from awm.drawio import recolour
from awm.drawio.renderspec import parse_swaps

Image = pytest.importorskip("PIL.Image")
np = pytest.importorskip("numpy")

MAGENTA = (0xFF, 0x00, 0xDC)
DARK = (0x21, 0x21, 0x21)
SWAP = parse_swaps(["ff00dc:212121"])

SVG = ('<svg xmlns="http://www.w3.org/2000/svg">'
       '<rect fill="#ff00dc"/><style>a{fill:#FF00DC;}</style></svg>')


def png_bytes(image, **kwargs) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", **kwargs)
    return buffer.getvalue()


def pixels(raw: bytes):
    return np.array(Image.open(io.BytesIO(raw)))


# --- the data URI codec ----------------------------------------------------

def test_drawios_own_import_shape_is_recognised():
    """drawio base64s an imported SVG but writes it in the *comma* form, with
    no `;base64` marker — the marker would be cut in half by the style
    splitter. Only the payload distinguishes it from a percent-encoded one."""
    uri = "data:image/svg+xml," + base64.b64encode(SVG.encode()).decode()
    out, hits, problems = recolour.swap_data_uri(uri, SWAP)
    assert hits == 2 and not problems
    assert base64.b64decode(out.split(",", 1)[1]).decode().count("#212121") == 2


def test_a_percent_encoded_payload_is_recognised_too():
    """The form this service writes when it inlines an image itself."""
    uri = "data:image/svg+xml," + quote(SVG, safe="")
    out, hits, _ = recolour.swap_data_uri(uri, SWAP)
    assert hits == 2
    assert "%23212121" in out and "%23ff00dc" not in out.lower()


def test_the_codec_it_arrived_in_is_the_codec_it_leaves_in():
    b64 = "data:image/svg+xml," + base64.b64encode(SVG.encode()).decode()
    pct = "data:image/svg+xml," + quote(SVG, safe="")
    assert "%" not in recolour.swap_data_uri(b64, SWAP)[0].split(",", 1)[1]
    assert "%" in recolour.swap_data_uri(pct, SWAP)[0]


def test_no_substitution_ever_writes_a_base64_marker():
    """drawio splits style strings on ';', so a `;base64,` URI truncates and
    the cell renders blank. Neither codec can emit one, and this is the guard
    that keeps it that way."""
    for uri in ("data:image/svg+xml," + base64.b64encode(SVG.encode()).decode(),
                "data:image/svg+xml," + quote(SVG, safe="")):
        assert ";" not in recolour.swap_data_uri(uri, SWAP)[0]


def test_a_marker_form_uri_keeps_its_marker():
    """Inside an SVG there is no style parser to trip over, so a payload that
    arrived with the marker keeps it rather than being silently reshaped."""
    uri = "data:image/svg+xml;base64," + base64.b64encode(SVG.encode()).decode()
    out, hits, _ = recolour.swap_data_uri(uri, SWAP)
    assert hits == 2 and out.startswith("data:image/svg+xml;base64,")


@pytest.mark.parametrize("value", [
    "/files/home/tony/fig.svg",
    "https://host/drawio-app/view/fig/a.drawio/plasmid?swap=ff00dc:00cc96",
    "shape=mxgraph.aws4.instance",
    "",
])
def test_anything_that_is_not_a_data_uri_comes_back_untouched(value):
    assert recolour.swap_data_uri(value, SWAP) == (value, 0, [])


def test_a_payload_that_matches_nothing_is_not_re_encoded():
    """The content key is a hash of the inlined document. Re-encoding an image
    that did not match would mint a new key for every embedded image on every
    render and invalidate the whole view cache."""
    uri = "data:image/svg+xml," + base64.b64encode(SVG.encode()).decode()
    assert recolour.swap_data_uri(uri, parse_swaps(["00ff00:112233"])) == (
        uri, 0, [])


# --- vector ----------------------------------------------------------------

def test_a_raster_nested_inside_an_imported_svg_is_reached():
    """How BioRender art actually arrives: an SVG wrapping base64 PNGs. The
    colours are in the pixels, not in the SVG source."""
    raster = png_bytes(Image.new("RGB", (4, 4), MAGENTA))
    wrapper = ('<svg xmlns="http://www.w3.org/2000/svg"><image href='
               '"data:image/png;base64,'
               + base64.b64encode(raster).decode() + '"/></svg>').encode()
    out, hits, problems = recolour.swap_bytes(wrapper, "image/svg+xml", SWAP)
    assert hits == 1 and not problems
    inner = base64.b64decode(out.split(b";base64,")[1].split(b'"')[0])
    assert tuple(pixels(inner)[0, 0])[:3] == DARK


# --- pixels ----------------------------------------------------------------

def test_a_flat_masked_region_lands_exactly_on_the_target():
    image = Image.new("RGB", (8, 8), (255, 255, 255))
    for x in range(4):
        for y in range(4):
            image.putpixel((x, y), MAGENTA)
    out, hits, _ = recolour.swap_bytes(png_bytes(image), "image/png", SWAP)
    grid = pixels(out)
    assert hits == 1
    assert tuple(grid[0, 0]) == DARK
    assert tuple(grid[6, 6]) == (255, 255, 255)


def test_shading_survives_because_the_shift_is_a_delta_not_a_fill():
    """A flat fill would flatten a textured region into a blob. The delta keeps
    the texture and still lands the flat part exactly on the target."""
    image = Image.new("RGB", (4, 1), MAGENTA)
    image.putpixel((1, 0), (0xF5, 0x0A, 0xD4))     # a shaded neighbour
    out, _, _ = recolour.swap_bytes(png_bytes(image), "image/png", SWAP)
    grid = pixels(out)
    assert tuple(grid[0, 0]) == DARK
    assert tuple(grid[0, 1]) != tuple(grid[0, 0])
    assert abs(int(grid[0, 1][0]) - DARK[0]) <= 12


def test_a_lossy_jpeg_still_matches_and_stays_a_jpeg():
    """A JPEG never reproduces a flat field exactly, so a zero tolerance would
    match nothing at all here."""
    image = Image.new("RGB", (32, 32), (255, 255, 255))
    for x in range(16):
        for y in range(16):
            image.putpixel((x, y), MAGENTA)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    out, hits, _ = recolour.swap_bytes(buffer.getvalue(), "image/jpeg", SWAP)
    assert hits == 1
    assert Image.open(io.BytesIO(out)).format == "JPEG"
    assert max(abs(int(c) - t) for c, t in zip(pixels(out)[4, 4], DARK)) <= 12


def test_alpha_is_carried_through_and_invisible_pixels_are_left_alone():
    image = Image.new("RGBA", (4, 4), MAGENTA + (255,))
    image.putpixel((3, 3), MAGENTA + (0,))
    out, _, _ = recolour.swap_bytes(png_bytes(image), "image/png", SWAP)
    grid = pixels(out)
    assert grid.shape[2] == 4
    assert tuple(grid[0, 0]) == DARK + (255,)
    assert tuple(grid[3, 3]) == MAGENTA + (0,)


def test_a_palette_image_has_its_palette_remapped():
    """Exact, and independent of how many pixels use the colour."""
    image = Image.new("P", (4, 4))
    image.putpalette(list(MAGENTA) + [255, 255, 255] + [0] * (254 * 3))
    out, hits, _ = recolour.swap_bytes(png_bytes(image), "image/png", SWAP)
    remapped = Image.open(io.BytesIO(out))
    assert hits == 1 and remapped.mode == "P"
    assert tuple(remapped.getpalette()[:3]) == DARK


def test_a_greyscale_image_is_returned_byte_identical():
    raw = png_bytes(Image.new("L", (4, 4), 128))
    assert recolour.swap_bytes(raw, "image/png", SWAP) == (raw, 0, [])


def test_two_swaps_never_compound_on_one_pixel():
    """The same simultaneity the text branch gets from a single alternation."""
    raw = png_bytes(Image.new("RGB", (4, 4), MAGENTA))
    out, _, _ = recolour.swap_bytes(
        raw, "image/png", parse_swaps(["ff00dc:00ff00", "00ff00:112233"]))
    assert tuple(pixels(out)[0, 0]) == (0, 255, 0)


def test_a_format_with_no_decoder_here_is_left_alone():
    assert recolour.swap_bytes(b"\x00\x01\x02", "application/pdf", SWAP) == (
        b"\x00\x01\x02", 0, [])


def test_an_image_that_cannot_be_decoded_is_reported():
    """A swap that matched nothing is not an error. A swap that could not be
    *attempted* is, and it has to reach X-Drawio-Problems rather than pass for
    a picture that simply had no mask in it."""
    _, hits, problems = recolour.swap_bytes(
        b"\x89PNG\r\n\x1a\n\xff\x00\xff", "image/png", SWAP)
    assert hits == 0 and problems


def test_a_missing_pillow_is_reported_not_skipped(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.startswith("PIL"):
            raise ImportError("no PIL here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    raw = b"\x89PNG\r\n\x1a\n"
    out, hits, problems = recolour.swap_bytes(raw, "image/png", SWAP)
    assert out == raw and hits == 0
    assert problems and "Pillow" in problems[0]


def test_one_problem_is_reported_once_however_many_images_hit_it():
    """A document can hold hundreds of images; the same complaint three hundred
    times is noise, not information."""
    sub = recolour.Substituter(SWAP)
    for _ in range(5):
        sub.sub_bytes(b"\x89PNG\r\n\x1a\n\xff\x00\xff", "image/png")
    assert len(sub.problems) == 1


def test_an_oversized_payload_is_refused_rather_than_decoded():
    payload = "data:image/svg+xml," + quote("#ff00dc" * 4_000_000, safe="")
    out, hits, problems = recolour.swap_data_uri(payload, SWAP)
    assert out == payload and hits == 0
    assert problems and "limit" in problems[0]

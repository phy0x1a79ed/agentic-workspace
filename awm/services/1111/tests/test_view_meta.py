import os
import sys

from PIL import Image
from PIL.PngImagePlugin import PngInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "view"))

import meta  # noqa: E402

FIELDS = 'Steps: 20, Sampler: Euler a, CFG scale: 7, Seed: 42, Size: 512x768, Model hash: abc, Model: base'


def test_prompt_negative_and_fields():
    m = meta.parse_parameters(f"a cat, sitting\nNegative prompt: blurry, dark\n{FIELDS}")
    assert m["prompt"] == "a cat, sitting"
    assert m["negative"] == "blurry, dark"
    assert m["params"]["Sampler"] == "Euler a"
    assert m["params"]["Size"] == "512x768"
    assert m["params"]["Model"] == "base"


def test_multiline_prompt_and_negative():
    m = meta.parse_parameters(f"line one\nline two\nNegative prompt: n1\nn2\n{FIELDS}")
    assert m["prompt"] == "line one\nline two"
    assert m["negative"] == "n1\nn2"


def test_missing_negative_prompt():
    m = meta.parse_parameters(f"just a prompt\n{FIELDS}")
    assert m["prompt"] == "just a prompt"
    assert m["negative"] == ""
    assert m["params"]["Seed"] == "42"


def test_quoted_value_keeps_its_commas():
    m = meta.parse_parameters(f'p\n{FIELDS}, Lora hashes: "one: 111, two: 222", Version: v1')
    assert m["params"]["Lora hashes"] == "one: 111, two: 222"
    assert m["params"]["Version"] == "v1"


def test_loras_from_tags_and_hashes():
    m = meta.parse_parameters(
        f'x <lora:one:0.6> y <lora:two:1.2> <lyco:three>\n{FIELDS}, Lora hashes: "one: 1, four: 4"')
    assert m["loras"] == [
        {"name": "one", "weight": 0.6},
        {"name": "two", "weight": 1.2},
        {"name": "three", "weight": 1.0},
        {"name": "four", "weight": None},
    ]


def test_text_without_field_line_is_all_prompt():
    m = meta.parse_parameters("only words here")
    assert m["prompt"] == "only words here"
    assert m["params"] == {}


def test_mode():
    assert meta.mode_for("txt2img-images/2024-01-01/a.png", {"Denoising strength": "0.5"}) == "txt2img"
    assert meta.mode_for("img2img-images/a.png", {}) == "img2img"
    assert meta.mode_for("lora/a.png", {"Denoising strength": "0.4"}) == "img2img"
    assert meta.mode_for("lora/a.png", {"Denoising strength": "0.4", "Hires upscale": "2"}) == "txt2img"


def _png(path, text=None):
    info = PngInfo()
    if text:
        info.add_text("parameters", text)
    Image.new("RGB", (8, 6)).save(path, pnginfo=info)


def test_read_image_png_chunk(tmp_path):
    p = tmp_path / "a.png"
    _png(p, f"hello\n{FIELDS}")
    r = meta.read_image(str(p), "txt2img-images/d/a.png")
    assert (r["width"], r["height"], r["error"]) == (8, 6, None)
    assert r["meta"]["prompt"] == "hello"
    assert r["meta"]["mode"] == "txt2img"


def test_read_image_sidecar_fallback(tmp_path):
    p = tmp_path / "b.png"
    _png(p)
    (tmp_path / "b.txt").write_text(f"from sidecar\n{FIELDS}")
    r = meta.read_image(str(p), "img2img-images/b.png")
    assert r["meta"]["prompt"] == "from sidecar"
    assert r["meta"]["mode"] == "img2img"


def test_read_image_corrupt_file(tmp_path):
    p = tmp_path / "c.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n garbage")
    r = meta.read_image(str(p), "c.png")
    assert r["error"]
    assert r["meta"] is None

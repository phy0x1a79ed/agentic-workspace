"""Converting embedded image payloads to filesystem references.

The correctness bar here is unusually high: this rewrites every image cell in a
document, and a wrong match swaps one molecule's structure for another's in a
figure that still *looks* right. So matching is by exact content hash and
anything ambiguous is left alone and reported.
"""

from __future__ import annotations

from urllib.parse import quote

from awm.drawio.externalize import externalize, index_files


def write_svg(path, body: str):
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg">{body}</svg>', encoding="utf-8")
    return path


def embedded(path) -> str:
    """The inline form a pre-service diagram carries."""
    return "data:image/svg+xml," + quote(path.read_text(encoding="utf-8"), safe="")


def test_matching_payload_becomes_a_reference(tmp_path):
    svg = write_svg(tmp_path / "glucose.svg", "<circle r='1'/>")
    xml = f'<mxCell style="shape=image;image={embedded(svg)};" />'

    out, report = externalize(xml, [tmp_path])
    assert report["converted"] == 1 and report["unmatched"] == 0
    assert f"/files{svg}" in out and "data:image" not in out


def test_unmatched_payload_is_left_embedded(tmp_path):
    """Better a large file than a wrong picture."""
    xml = ('<mxCell style="image=data:image/svg+xml,'
           + quote("<svg xmlns='http://www.w3.org/2000/svg'/>", safe="") + ';" />')
    out, report = externalize(xml, [tmp_path])
    assert report["converted"] == 0 and report["unmatched"] == 1
    assert "data:image/svg+xml," in out


def test_near_miss_does_not_match(tmp_path):
    """A one-character difference is a different molecule, not a close enough one."""
    on_disk = write_svg(tmp_path / "a.svg", "<circle r='1'/>")
    xml = ('<mxCell style="image=data:image/svg+xml,'
           + quote("<svg xmlns='http://www.w3.org/2000/svg'><circle r='2'/></svg>",
                   safe="") + ';" />')
    out, report = externalize(xml, [tmp_path])
    assert report["converted"] == 0
    assert str(on_disk) not in out


def test_shared_payload_maps_to_one_deterministic_file(tmp_path):
    """Identical files must resolve the same way every run, or re-running a
    migration produces a diff that means nothing."""
    first = write_svg(tmp_path / "aaa.svg", "<rect/>")
    write_svg(tmp_path / "zzz.svg", "<rect/>")
    xml = f'<mxCell style="image={embedded(first)};" />' * 3

    out_a, report_a = externalize(xml, [tmp_path])
    out_b, report_b = externalize(xml, [tmp_path])
    assert out_a == out_b
    assert report_a["converted"] == 3 and len(report_a["files"]) == 1


def test_reference_form_has_no_semicolon(tmp_path):
    """The output must survive drawio's style parser, which splits on ';'."""
    svg = write_svg(tmp_path / "m.svg", "<style>.a{fill:#f00;stroke:#000}</style>")
    xml = f'<mxCell style="shape=image;image={embedded(svg)};" />'
    out, _ = externalize(xml, [tmp_path])

    reference = out.split("image=")[1].split(";")[0]
    assert reference == f"/files{svg}"


def test_report_measures_what_was_saved(tmp_path):
    svg = write_svg(tmp_path / "big.svg", "<rect/>" * 500)
    xml = f'<mxCell style="image={embedded(svg)};" />'
    _, report = externalize(xml, [tmp_path])
    assert report["bytes_saved"] > 1000


def test_index_skips_non_images(tmp_path):
    write_svg(tmp_path / "keep.svg", "<rect/>")
    (tmp_path / "notes.txt").write_text("not an image", encoding="utf-8")
    index = index_files([tmp_path])
    assert len(index) == 1


def test_missing_search_root_is_tolerated(tmp_path):
    svg = write_svg(tmp_path / "m.svg", "<rect/>")
    xml = f'<mxCell style="image={embedded(svg)};" />'
    out, report = externalize(xml, [tmp_path / "nope", tmp_path])
    assert report["converted"] == 1 and f"/files{svg}" in out

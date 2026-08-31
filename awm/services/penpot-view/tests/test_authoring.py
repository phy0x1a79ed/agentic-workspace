"""The wire shape of a change vector, pinned.

Everything here is a rule Penpot enforces on the far side and reports badly:
a plain map where a record was wanted comes back as ``:data-validation`` and
a shape id, and a geometry that disagrees with itself comes back as nothing
at all. None of it touches a network -- these are pure builders.
"""

from __future__ import annotations

import json
import uuid as uuidlib

import pytest

from awm.penpot_view import authoring as A
from awm.penpot_view import exporter_client as EC

BOARD = uuidlib.UUID("0197f9d2-1a2b-73aa-8b9c-1234567890b0")
PAGE = uuidlib.UUID("0197f9d2-1a2b-73aa-8b9c-1234567890b1")
FILE = uuidlib.UUID("0197f9d2-1a2b-73aa-8b9c-1234567890b2")
COMPONENT = uuidlib.UUID("0197f9d2-1a2b-73aa-8b9c-1234567890b3")


def _wire(value) -> dict:
    return json.loads(EC.transit_dumps(value))


def test_kmap_spells_attributes_the_way_penpot_does():
    assert _wire(A.kmap(font_size="16", grow_type=None)) == {
        "~:font-size": "16", "~:grow-type": None}


def test_a_shape_goes_on_the_wire_as_a_record_not_a_map():
    wire = _wire(A.shape(id=BOARD, name="Badge", type="frame",
                         x=0, y=0, width=10, height=20,
                         parent_id=BOARD, frame_id=BOARD, shapes=[]))
    # `~#shape` is what makes the far side build a Shape record, which is the
    # first predicate `cts/valid-shape?` applies.
    assert list(wire) == ["~#shape"]
    body = wire["~#shape"]
    assert body["~:type"] == "~:frame"
    assert list(body["~:selrect"]) == ["~#rect"]
    assert list(body["~:transform"]) == ["~#matrix"]
    assert [list(p) for p in body["~:points"]] == [["~#point"]] * 4


def test_the_four_statements_of_one_boxs_geometry_agree():
    body = _wire(A.shape(id=BOARD, name="B", type="rect", x=5, y=7,
                         width=100, height=50, parent_id=BOARD,
                         frame_id=BOARD))["~#shape"]
    sel = body["~:selrect"]["~#rect"]
    assert (sel["~:x"], sel["~:y"]) == (5, 7)
    assert (sel["~:x2"], sel["~:y2"]) == (105, 57)
    corners = [(p["~#point"]["~:x"], p["~#point"]["~:y"])
               for p in body["~:points"]]
    assert corners == [(5, 7), (105, 7), (105, 57), (5, 57)]


def test_a_change_type_is_a_keyword_and_a_text_node_type_is_not():
    change = _wire(A.add_obj(A.shape(id=BOARD, name="B", type="rect", x=0,
                                     y=0, width=1, height=1,
                                     parent_id=BOARD, frame_id=BOARD),
                             id=BOARD, page_id=PAGE, frame_id=BOARD))
    assert change["~:type"] == "~:add-obj"
    content = _wire(A.text_content("hi", font_id="gfont-work-sans",
                                   font_family="Work Sans"))
    assert content["~:type"] == "root"
    para_set = content["~:children"][0]
    assert para_set["~:type"] == "paragraph-set"
    assert para_set["~:children"][0]["~:children"][0]["~:text"] == "hi"


def test_a_text_node_carries_the_font_id_the_render_loads_by():
    leaf = _wire(A.text_content("hi", font_id="gfont-work-sans",
                                font_family="Work Sans")
                 )["~:children"][0]["~:children"][0]["~:children"][0]
    # font-family alone names a CSS family nothing ever fetched.
    assert leaf["~:font-id"] == "gfont-work-sans"
    assert leaf["~:font-variant-id"] == "regular"
    assert leaf["~:font-family"] == "Work Sans"


def test_a_position_run_states_a_baseline_and_a_width_to_stretch_to():
    run = _wire(A.text_position("hi", x=10, y=30, width=80, height=20,
                                font_family="Work Sans"))[0]
    assert (run["~:x"], run["~:y"], run["~:width"]) == (10, 30, 80)
    assert run["~:text"] == "hi"


def test_add_obj_defaults_the_parent_to_the_frame():
    change = _wire(A.add_obj(A.shape(id=BOARD, name="B", type="rect", x=0,
                                     y=0, width=1, height=1,
                                     parent_id=BOARD, frame_id=BOARD),
                             id=BOARD, page_id=PAGE, frame_id=BOARD))
    assert change["~:parent-id"] == change["~:frame-id"]
    assert "~:ignore-touched" not in change


def test_a_set_operation_names_its_attribute_as_a_keyword():
    op = _wire(A.set_op("component-root", None))
    assert op == {"~:type": "~:set", "~:attr": "~:component-root",
                  "~:val": None}


def test_add_page_never_carries_a_page_beside_a_name():
    # The handler raises :conflict when id+name and page both arrive.
    assert set(_wire(A.add_page(PAGE, "Reuse"))) == {
        "~:type", "~:id", "~:name"}


def test_the_main_instance_markup_names_the_component_not_the_board():
    ops = {o["~:attr"]: o["~:val"]
           for o in _wire(A.main_instance_ops(component_id=COMPONENT,
                                              file_id=FILE))}
    assert ops["~:component-id"] == f"~u{COMPONENT}"
    assert ops["~:component-file"] == f"~u{FILE}"
    assert ops["~:main-instance"] is True
    assert ops["~:component-root"] is True
    # A main instance is not a copy: it must not carry a shape-ref.
    assert ops["~:shape-ref"] is None


def test_an_image_fill_carries_only_the_keys_the_closed_schema_allows():
    media = uuidlib.uuid4()
    fill = _wire(A.image_fill(id=media, width=96, height=96,
                              mtype="image/png", name="demo"))
    assert set(fill["~:fill-image"]) == {
        "~:id", "~:width", "~:height", "~:mtype", "~:name"}


def test_the_encoder_still_refuses_a_type_it_does_not_know():
    with pytest.raises(TypeError):
        EC.transit_dumps({A.Keyword("x"): object()})

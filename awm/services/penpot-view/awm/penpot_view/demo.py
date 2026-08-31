"""The demo the box keeps: one drawing, reused as a component, twice over.

Page *Source* holds a board that is the main instance of a component. Page
*Reuse* holds a copy of it. Editing the board changes the copy, because
Penpot syncs copies against their main within a file, and the vault note
embeds a render of the copy -- so an edit on page Source reaches the note
with nothing else done.

Running this twice produces one file, not two, and prints the same two
``file/page/board`` triples both times. Every id is derived with
:func:`uuid.uuid5` from a fixed namespace, and every step reads the file
back and skips itself if its own object is already there. Idempotency is
never keyed on ``revn``: that is a concurrency token and it moves whenever
anybody edits anything.

The four writes are deliberately four separate ``update-file`` calls rather
than one. The protocol is happy with one; the debugging is not. A rejected
change vector names ``:data-validation`` and the offending shape id and
nothing more, so a failure that arrives on its own is worth a great deal
more than a failure that arrives with three unrelated kinds of change
attached to it.
"""

from __future__ import annotations

import logging
import os
import struct
import uuid
import zlib
from collections.abc import Mapping
from typing import Any

from . import authoring as A
from .exporter_client import ExporterClient, ExporterError

log = logging.getLogger("awm.penpot_view.demo")

#: Every id below is derived from here, so the demo's triples survive a
#: rebuild of the box and can be written into a note by hand if they have to
#: be. Changing this string orphans the previous demo rather than editing it.
NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://awm/penpot-view/demo/v1")

#: Penpot's root frame. Every page has one, with this id, and a shape whose
#: parent is the page itself names this rather than nothing.
ROOT_FRAME = uuid.UUID(int=0)

DEFAULT_TEAM = os.environ.get("PENPOT_SHARED_TEAM") or "Shared"
PROJECT_NAME = "Demo"
FILE_NAME = "Chain demo"
SOURCE_PAGE = "Source"
REUSE_PAGE = "Reuse"
COMPONENT_NAME = "Badge"

#: A Google font, on purpose: the render has to reach Penpot's own gfonts
#: proxy for it, which is the sub-resource path that a wrong origin breaks
#: silently. A board of plain shapes in the default font proves nothing.
FONT_FAMILY = "Work Sans"
FONT_ID = "gfont-work-sans"

BOARD_W, BOARD_H = 320, 220
COPY_AT = (40.0, 40.0)


def _id(*parts: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, "/".join(parts))


PROJECT_ID = _id("project")
FILE_ID = _id("file")
REUSE_PAGE_ID = _id("page", "reuse")
COMPONENT_ID = _id("component")
#: The board, and the three shapes on it. The copy's ids are derived from the
#: same names so a reader can pair them up.
MAIN_IDS = {name: _id("main", name)
            for name in ("board", "ribbon", "photo", "label")}
COPY_IDS = {name: _id("copy", name) for name in MAIN_IDS}


# --- the picture -----------------------------------------------------------

def demo_png(size: int = 96) -> bytes:
    """A small deterministic PNG, built here rather than shipped as a blob.

    The demo needs an image *fill* -- that is the only thing that exercises
    Penpot's ``/assets/`` path on the way out, and a render that quietly
    drops its images is served 200 with the picture missing. Generating it
    keeps the dist free of a binary asset and keeps the bytes identical on
    every box, which is what makes a re-upload detectable.
    """
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # PNG per-row filter: none
        for x in range(size):
            band = ((x + y) // 12) % 2
            rows += (bytes((123, 97, 255)) if band
                     else bytes((236, 231, 255)))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + chunk(b"IEND", b""))


# --- lookups ---------------------------------------------------------------

def _find(rows: Any, **match: Any) -> Mapping | None:
    if not isinstance(rows, (list, tuple)):
        return None
    for row in rows:
        if isinstance(row, Mapping) and all(
                row.get(k) == v for k, v in match.items()):
            return row
    return None


def _team(client: ExporterClient, name: str) -> Mapping:
    teams = client.rpc("get-teams")
    found = [t for t in (teams or []) if isinstance(t, Mapping)
             and t.get("name") == name]
    if not found:
        have = ", ".join(sorted(str(t.get("name")) for t in (teams or [])
                                if isinstance(t, Mapping)))
        raise ExporterError(
            f"no penpot team named {name!r} for this profile (has: {have}). "
            "Run scripts/sirius/penpot-team.sh, which is what creates the "
            "shared team and puts everyone in it.")
    if len(found) > 1:
        raise ExporterError(
            f"{len(found)} penpot teams are named {name!r}; Penpot puts no "
            "unique constraint on a team name, so this has to be resolved by "
            "hand before the demo can pick one.")
    return found[0]


def _project(client: ExporterClient, team_id: uuid.UUID) -> uuid.UUID:
    projects = client.rpc("get-projects", {A.Keyword("team-id"): team_id})
    existing = _find(projects, id=PROJECT_ID)
    if existing is None and _find(projects, name=PROJECT_NAME) is not None:
        # A project of that name that is not ours: leave it alone and put the
        # demo in its own, rather than writing into somebody else's folder.
        log.info("penpot-view: a project named %r already exists and is not "
                 "the demo's; creating the demo's own", PROJECT_NAME)
    if existing is None:
        client.rpc("create-project", {A.Keyword("team-id"): team_id,
                                      A.Keyword("name"): PROJECT_NAME,
                                      A.Keyword("id"): PROJECT_ID})
        log.info("penpot-view: created demo project %s", PROJECT_ID)
    return PROJECT_ID


def _file(client: ExporterClient, project_id: uuid.UUID) -> None:
    files = client.rpc("get-project-files",
                       {A.Keyword("project-id"): project_id})
    if _find(files, id=FILE_ID) is None:
        client.rpc("create-file", {A.Keyword("project-id"): project_id,
                                   A.Keyword("name"): FILE_NAME,
                                   A.Keyword("id"): FILE_ID})
        log.info("penpot-view: created demo file %s", FILE_ID)


def _get_file(client: ExporterClient) -> Mapping:
    """Read the file back.

    No ``:features``. Two reasons, both load-bearing: an empty feature set
    keeps the encoder off the transit set type it does not implement, and it
    makes the backend realise the objects map into plain maps rather than
    handing back the pointer-map form this decoder cannot follow.
    """
    file = client.rpc("get-file", {A.Keyword("id"): FILE_ID})
    if not isinstance(file, Mapping) or not isinstance(file.get("data"), Mapping):
        raise ExporterError(f"get-file for the demo returned no data: {file!r}")
    return file


# --- the four change vectors -----------------------------------------------

def _geometry(page_id: uuid.UUID, media: Mapping) -> list:
    """The board and the three shapes on it, in parent-before-child order."""
    board, ribbon, photo, label = (MAIN_IDS[k] for k in
                                   ("board", "ribbon", "photo", "label"))
    changes = [
        A.add_obj(
            A.shape(id=board, name=COMPONENT_NAME, type="frame",
                    x=0, y=0, width=BOARD_W, height=BOARD_H,
                    parent_id=ROOT_FRAME, frame_id=ROOT_FRAME,
                    shapes=[], fills=[A.solid_fill("#FFFFFF")],
                    show_content=True),
            id=board, page_id=page_id, frame_id=ROOT_FRAME,
            parent_id=ROOT_FRAME),
        A.add_obj(
            A.shape(id=ribbon, name="Ribbon", type="rect",
                    x=0, y=0, width=BOARD_W, height=56,
                    parent_id=board, frame_id=board,
                    fills=[A.solid_fill("#7B61FF")]),
            id=ribbon, page_id=page_id, frame_id=board, parent_id=board),
        A.add_obj(
            A.shape(id=photo, name="Photo", type="rect",
                    x=24, y=88, width=96, height=96,
                    parent_id=board, frame_id=board,
                    r1=8, r2=8, r3=8, r4=8,
                    fills=[A.image_fill(id=media["id"],
                                        width=int(media["width"]),
                                        height=int(media["height"]),
                                        mtype=str(media["mtype"]),
                                        name=str(media.get("name") or "demo"))]),
            id=photo, page_id=page_id, frame_id=board, parent_id=board),
        A.add_obj(
            _label_shape(label, board),
            id=label, page_id=page_id, frame_id=board, parent_id=board),
    ]
    return changes


LABEL_TEXT = "awm x penpot"
LABEL_BOX = (144.0, 100.0, 152.0, 26.0)
LABEL_SIZE = "20"


def _label_shape(shape_id: uuid.UUID, parent: uuid.UUID) -> Any:
    x, y, w, h = LABEL_BOX
    return A.shape(
        id=shape_id, name="Label", type="text",
        x=x, y=y, width=w, height=h, parent_id=parent, frame_id=parent,
        grow_type=A.Keyword("fixed"),
        content=A.text_content(LABEL_TEXT, font_id=FONT_ID,
                               font_family=FONT_FAMILY,
                               font_size=LABEL_SIZE),
        # The baseline sits a font-size below the top of the box; see
        # authoring.text_position on why one run is enough.
        position_data=A.text_position(
            LABEL_TEXT, x=x, y=y + float(LABEL_SIZE), width=w, height=h,
            font_family=FONT_FAMILY, font_size=LABEL_SIZE))


def _component(page_id: uuid.UUID) -> list:
    board = MAIN_IDS["board"]
    changes = [
        A.add_component(COMPONENT_ID, name=COMPONENT_NAME, path="",
                        main_instance_id=board, main_instance_page=page_id),
        A.mod_obj(board, page_id=page_id,
                  operations=A.main_instance_ops(component_id=COMPONENT_ID,
                                                 file_id=FILE_ID)),
    ]
    # Penpot clears :component-root on everything under a main instance. The
    # children never had it, but stating it keeps the file identical to one
    # the editor would have produced, which is what makes a later diff
    # readable.
    changes += [A.mod_obj(MAIN_IDS[name], page_id=page_id,
                          operations=[A.set_op("component-root", None)])
                for name in ("ribbon", "photo", "label")]
    return changes


def _copy(objects: Mapping, page_id: uuid.UUID) -> list:
    """A copy of the main instance, on the second page.

    Every shape is cloned with a new id and a ``:shape-ref`` back to the one
    it was cloned from; only the root carries ``:component-id`` /
    ``:component-file`` / ``:component-root``. That pairing is what
    ``sync-file`` walks after an edit to the main, so a copy assembled
    without it looks identical in the editor and never changes again.
    """
    dx, dy = COPY_AT
    changes = []
    for name in ("board", "ribbon", "photo", "label"):
        src = objects.get(MAIN_IDS[name])
        if not isinstance(src, Mapping):
            raise ExporterError(
                f"the demo's {name!r} shape is missing from the source page; "
                "the geometry changeset did not land")
        new_id = COPY_IDS[name]
        root = name == "board"
        parent = ROOT_FRAME if root else COPY_IDS["board"]
        extra: dict = {"shape_ref": MAIN_IDS[name]}
        if root:
            extra.update(component_id=COMPONENT_ID, component_file=FILE_ID,
                         component_root=True,
                         shapes=[COPY_IDS[n] for n in
                                 ("ribbon", "photo", "label")])
        for attr in ("fills", "content", "position-data", "r1", "r2", "r3",
                     "r4", "grow-type", "show-content"):
            if src.get(attr) is not None:
                extra[attr.replace("-", "_")] = src[attr]
        if name == "label":
            # position-data carries absolute coordinates of its own, so the
            # copy's run has to move with the shape or the text renders back
            # at the main instance's position.
            extra["position_data"] = [
                {k: (v + dx if k == "x" else v + dy if k == "y" else v)
                 for k, v in run.items()}
                for run in extra.get("position_data", [])]
        changes.append(A.add_obj(
            A.shape(id=new_id, name=str(src.get("name") or name),
                    type=str(src.get("type") or "rect"),
                    x=float(src["x"]) + dx, y=float(src["y"]) + dy,
                    width=float(src["width"]), height=float(src["height"]),
                    parent_id=parent,
                    frame_id=ROOT_FRAME if root else COPY_IDS["board"],
                    **extra),
            id=new_id, page_id=page_id, frame_id=(
                ROOT_FRAME if root else COPY_IDS["board"]),
            parent_id=parent, ignore_touched=True))
    return changes


# --- the seed ---------------------------------------------------------------

def _apply(client: ExporterClient, file: Mapping, changes: list,
           what: str) -> Mapping:
    """Send one change vector and read the file back.

    Read back rather than trust the response: ``update-file`` answers with
    the changes it accepted, which is not the same question as whether the
    file now holds what was intended. Every caller here decides what to do
    next from the file's own contents.
    """
    session_id = uuid.uuid4()
    log.info("penpot-view: demo %s -- %d change(s)", what, len(changes))
    client.rpc("update-file", {
        A.Keyword("id"): FILE_ID,
        A.Keyword("session-id"): session_id,
        A.Keyword("revn"): int(file["revn"]),
        A.Keyword("vern"): int(file["vern"]),
        A.Keyword("changes"): changes,
    })
    return _get_file(client)


def seed(client: ExporterClient, *, team: str | None = None) -> dict:
    """Create the demo if it is not there, and answer where it is."""
    team_name = team or DEFAULT_TEAM
    team_row = _team(client, team_name)
    project_id = _project(client, team_row["id"])
    _file(client, project_id)
    file = _get_file(client)
    data = file["data"]

    pages = list(data.get("pages") or [])
    if not pages:
        raise ExporterError("the demo file has no pages, which cannot happen "
                            "for a file Penpot created")
    source_page = pages[0]
    index = data.get("pages-index") or {}
    created = []

    if (index.get(source_page) or {}).get("name") != SOURCE_PAGE:
        file = _apply(client, file, [A.mod_page(source_page, name=SOURCE_PAGE)],
                      "naming the source page")
        data = file["data"]
        index = data.get("pages-index") or {}

    objects = (index.get(source_page) or {}).get("objects") or {}
    if MAIN_IDS["board"] not in objects:
        media = client.upload_media(
            file_id=FILE_ID, name="Demo swatch", filename="demo.png",
            data=demo_png(), mtype="image/png")
        file = _apply(client, file, _geometry(source_page, media), "geometry")
        data = file["data"]
        created.append("geometry")

    if COMPONENT_ID not in (data.get("components") or {}):
        file = _apply(client, file, _component(source_page), "component")
        data = file["data"]
        created.append("component")

    if REUSE_PAGE_ID not in (data.get("pages-index") or {}):
        file = _apply(client, file, [A.add_page(REUSE_PAGE_ID, REUSE_PAGE)],
                      "reuse page")
        data = file["data"]
        created.append("page")

    index = data.get("pages-index") or {}
    reuse_objects = (index.get(REUSE_PAGE_ID) or {}).get("objects") or {}
    if COPY_IDS["board"] not in reuse_objects:
        source_objects = (index.get(source_page) or {}).get("objects") or {}
        file = _apply(client, file, _copy(source_objects, REUSE_PAGE_ID),
                      "copy")
        created.append("copy")

    return {
        "team": team_name,
        "team_id": str(team_row["id"]),
        "file_id": str(FILE_ID),
        "source": {"page_id": str(source_page),
                   "board_id": str(MAIN_IDS["board"])},
        "reuse": {"page_id": str(REUSE_PAGE_ID),
                  "board_id": str(COPY_IDS["board"])},
        "created": created,
    }

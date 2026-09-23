"""Gallery server for the webui's output images.

Runs as the account that owns those images, behind a gateway prefix that it
learns from `X-Forwarded-Prefix`. Files are served by index id only.
"""
import asyncio
import html
import logging
import os
import threading
import time

from fastapi import Body, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

from store import Store, StoreError

log = logging.getLogger("view")

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
THUMB_BOX = (640, 400)
RESCAN_S = 60


def _ids(payload):
    return [int(i) for i in payload.get("ids") or []]


def create_app(outputs_dir=None, data_dir=None, rescan_s=RESCAN_S):
    outputs_dir = outputs_dir or os.environ.get(
        "VIEW_OUTPUTS", os.path.expanduser("~/stable-diffusion-webui/outputs"))
    data_dir = data_dir or os.environ.get("VIEW_DATA", os.path.expanduser("~/view-data"))
    thumbs_dir = os.path.join(data_dir, "thumbs")

    def thumb_path(image_id):
        return os.path.join(thumbs_dir, f"{image_id}-{THUMB_BOX[1]}.webp")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    state = {"store": None, "task": None, "last_scan": None, "scanning": False}
    scan_lock = threading.Lock()

    def store():
        return state["store"]

    def scan_once():
        if not scan_lock.acquire(blocking=False):
            return {"busy": True}
        state["scanning"] = True
        try:
            started = time.time()
            stats = store().scan()
            stats["seconds"] = round(time.time() - started, 2)
            state["last_scan"] = {**stats, "at": time.time()}
            return stats
        finally:
            state["scanning"] = False
            scan_lock.release()

    async def rescan_loop():
        while True:
            try:
                await run_in_threadpool(scan_once)
            except Exception:
                log.exception("scan failed")
            await asyncio.sleep(rescan_s)

    @app.on_event("startup")
    async def _startup():
        os.makedirs(thumbs_dir, exist_ok=True)
        state["store"] = Store(outputs_dir, data_dir)
        state["task"] = asyncio.get_event_loop().create_task(rescan_loop())

    @app.on_event("shutdown")
    async def _shutdown():
        if state["task"]:
            state["task"].cancel()
        if state["store"]:
            state["store"].close()

    @app.exception_handler(StoreError)
    async def _store_error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as fh:
            page = fh.read()
        version = str(int(max(os.path.getmtime(os.path.join(STATIC, f)) for f in os.listdir(STATIC))))
        page = page.replace("{{BASE}}", html.escape(prefix + "/", quote=True)).replace("{{V}}", version)
        return HTMLResponse(page, headers={"Cache-Control": "no-cache"})

    # ---- reading --------------------------------------------------------

    @app.get("/api/tree")
    def tree():
        s = store()
        return {"dirs": s.tree(), "folders": s.folders(), "trash": s.trash_count(),
                "scanning": state["scanning"], "last_scan": state["last_scan"]}

    @app.get("/api/images")
    def images(source: str = "dir:", offset: int = 0, limit: int = 200):
        return store().list_images(source, offset, min(max(limit, 1), 1000))

    @app.get("/api/image/{image_id}")
    def image(image_id: int):
        return store().get_image(image_id)

    @app.get("/file/{image_id}")
    def original(image_id: int):
        path = store().file_path(image_id)
        if not os.path.isfile(path):
            return Response(status_code=404)
        return FileResponse(path)

    @app.get("/thumb/{image_id}")
    def thumb(image_id: int):
        src = store().file_path(image_id)
        dest = thumb_path(image_id)
        try:
            fresh = os.path.getmtime(dest) >= os.path.getmtime(src)
        except OSError:
            fresh = False
        if not fresh:
            try:
                with Image.open(src) as im:
                    im.thumbnail(THUMB_BOX)
                    tmp = dest + ".tmp"
                    im.convert("RGB").save(tmp, "WEBP", quality=80)
                    os.replace(tmp, dest)
            except Exception:
                return Response(status_code=404)
        return FileResponse(dest, media_type="image/webp",
                            headers={"Cache-Control": "private, max-age=604800"})

    # ---- folders --------------------------------------------------------

    @app.post("/api/folders")
    def create_folder(payload: dict = Body(...)):
        return {"id": store().create_folder(payload.get("name"), payload.get("parent_id"))}

    @app.patch("/api/folders/{folder_id}")
    def update_folder(folder_id: int, payload: dict = Body(...)):
        if "name" in payload:
            store().rename_folder(folder_id, payload["name"])
        if "parent_id" in payload:
            store().reparent_folder(folder_id, payload["parent_id"])
        return {"ok": True}

    @app.delete("/api/folders/{folder_id}")
    def delete_folder(folder_id: int):
        store().delete_folder(folder_id)
        return {"ok": True}

    @app.post("/api/folders/{folder_id}/add")
    def add(folder_id: int, payload: dict = Body(...)):
        return {"added": store().add_to_folder(folder_id, _ids(payload))}

    @app.post("/api/folders/{folder_id}/remove")
    def remove(folder_id: int, payload: dict = Body(...)):
        return {"removed": store().remove_from_folder(folder_id, _ids(payload))}

    @app.post("/api/folders/{folder_id}/move")
    def move(folder_id: int, payload: dict = Body(...)):
        return {"added": store().move_between_folders(int(payload["from"]), folder_id, _ids(payload))}

    # ---- trash ----------------------------------------------------------

    @app.post("/api/trash")
    def trash(payload: dict = Body(...)):
        return {"trashed": store().trash(_ids(payload))}

    @app.post("/api/restore")
    def restore(payload: dict = Body(...)):
        return store().restore(_ids(payload))

    @app.post("/api/purge")
    def purge(payload: dict = Body(...)):
        ids = _ids(payload) if payload.get("ids") is not None else None
        targets = ids if ids is not None else [i["id"] for i in store().list_images("trash", 0, 10**9)["items"]]
        purged = store().purge(ids)
        for image_id in targets:
            try:
                os.remove(thumb_path(image_id))
            except OSError:
                pass
        return {"purged": purged}

    @app.post("/api/rescan")
    def rescan():
        return scan_once()

    return app


app = create_app()

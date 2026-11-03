"""The approver's own origin: the panel pages and the API they call (queue.md R64, R65).

deneb forwards this origin's TLS unopened, so only alcor sees what passes here. Reads
carry a device signature in three headers. Writes are chain messages.
"""

from __future__ import annotations

import ipaddress
from importlib import resources
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from awm.approver.core import Approver, ApproverError

STATIC = resources.files("awm.approver") / "static"
PAGES = {"/panel": "panel.html", "/panel/button": "button.html", "/enrol": "enrol.html",
         "/login": "login.html"}
ASSETS = {"panel.js", "panel.css", "approver.js"}
STATUS = {"need_passkey": 401, "locked": 423, "stale_head": 409, "unknown_device": 403,
          "bad_signature": 403, "stale": 403, "unknown_passkey": 403, "bad_passkey": 403, "not_allowed": 403,
          "unknown_request": 404}


def _client_ip(request: Request, forwarded_header: str | None) -> str | None:
    """The client's address, or None when it is unknown. Unknown never counts as trusted."""
    host = request.client.host if request.client else None
    try:
        loopback = host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if loopback and forwarded_header:
        return request.headers.get(forwarded_header) or None
    return None if loopback else host


def create_app(ap: Approver, *, board_origins: list[str] | None = None,
               forwarded_header: str | None = None,
               usage: Callable[[str], Awaitable[None]] | None = None) -> FastAPI:
    app = FastAPI(title="awm approver", docs_url=None, redoc_url=None, openapi_url=None)
    frame_ancestors = " ".join(["'self'", *(board_origins or [])])
    allow = " ".join(["self", *(f'"{o}"' for o in board_origins or [])])

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp: Response = await call_next(request)
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            f"connect-src 'self'; frame-ancestors {frame_ancestors}; base-uri 'none'; "
            "form-action 'self'")
        resp.headers["Permissions-Policy"] = (f"publickey-credentials-get=({allow}), "
                                              f"publickey-credentials-create=({allow})")
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(ApproverError)
    async def refused(request: Request, exc: ApproverError):
        return JSONResponse(exc.as_dict(), status_code=STATUS.get(exc.code, 400))

    def page(path: str, name: str):
        async def serve() -> FileResponse:
            if usage is not None:
                await usage(name.removesuffix(".html"))
            return FileResponse(str(STATIC / name), media_type="text/html")
        app.add_api_route(path, serve, methods=["GET"])

    for path, name in PAGES.items():
        page(path, name)

    @app.get("/static/{name}")
    async def asset(name: str):
        if name not in ASSETS:
            return JSONResponse({"error": "not found"}, status_code=404)
        kind = "text/css" if name.endswith(".css") else "text/javascript"
        return FileResponse(str(STATIC / name), media_type=kind)

    @app.get("/api/keys")
    async def keys():
        return {"ack_pub": ap.keys.ack_pub_spki_b64u(), "grant_pub": ap.keys.grant_pub_b64u(),
                "rp_id": ap.config.rp_id, "origin": ap.config.origin,
                "board_origins": board_origins or []}

    async def body(request: Request) -> dict[str, Any]:
        data = await request.json()
        if not isinstance(data, dict):
            raise ApproverError("bad_request", "the body is a JSON object")
        return data

    # -- enrolment ---------------------------------------------------------------

    @app.post("/api/enrol/begin")
    async def enrol_begin(request: Request):
        b = await body(request)
        return ap.enrol_begin(b.get("token", ""), str(b.get("name", ""))[:60])

    @app.post("/api/enrol/finish")
    async def enrol_finish(request: Request):
        b = await body(request)
        return ap.enrol_finish(b.get("token", ""), b.get("reg_id", ""), b.get("credential") or {},
                               b.get("device_pub", ""), str(b.get("name", ""))[:60],
                               _client_ip(request, forwarded_header))

    @app.post("/api/enrol/ask")
    async def enrol_ask(request: Request):
        b = await body(request)
        return await ap.enrol_ask(str(b.get("name", ""))[:60])

    @app.post("/api/enrol/status")
    async def enrol_status(request: Request):
        b = await body(request)
        return ap.enrol_ask_status(b.get("request_id", ""), b.get("poll_secret", ""))

    # -- the chain ------------------------------------------------------------------

    @app.post("/api/passkey/options")
    async def passkey_options(request: Request):
        """Assertion options for a request whose challenge binds ``prev`` and the request."""
        from awm.approver.chain import passkey_challenge
        b = await body(request)
        try:
            challenge = passkey_challenge(b.get("prev", ""), b.get("request") or {})
        except ValueError:
            raise ApproverError("bad_request", "prev is a hex head") from None
        return ap.passkey_options(challenge)

    @app.post("/api/act")
    async def act(request: Request):
        b = await body(request)
        return await ap.act(b.get("message") or {}, b.get("sig", ""), b.get("assertion"),
                            _client_ip(request, forwarded_header))

    @app.post("/api/resume")
    async def resume(request: Request):
        b = await body(request)
        return await ap.resume(b.get("device", ""), b.get("acked_head", ""), int(b.get("ts", 0)),
                               b.get("sig", ""), b.get("pending"),
                               _client_ip(request, forwarded_header))

    @app.post("/api/unlock/options")
    async def unlock_options(request: Request):
        b = await body(request)
        return ap.unlock_options(b.get("device", ""))

    @app.post("/api/unlock")
    async def unlock(request: Request):
        b = await body(request)
        return await ap.unlock(b.get("device", ""), b.get("assertion") or {}, b.get("sig", ""))

    # -- signed reads ------------------------------------------------------------------

    def reader(request: Request) -> str:
        h = request.headers
        try:
            ts = int(h.get("x-awm-ts", "0"))
        except ValueError:
            ts = 0
        dev = ap.check_read(h.get("x-awm-device", ""), ts, request.url.path,
                            h.get("x-awm-sig", ""))
        return dev["device_id"]

    @app.get("/api/card/{task_id}")
    async def card(task_id: str, request: Request):
        reader(request)
        return await ap.tools.store("task_get", {"task_id": task_id})

    @app.get("/api/request/{request_id}")
    async def request_get(request_id: str, request: Request):
        reader(request)
        return ap.approval_get(request_id)

    @app.get("/api/twofa")
    async def twofa(request: Request):
        reader(request)
        return await ap.twofa_state()

    @app.get("/api/windows")
    async def windows(request: Request):
        return ap.windows(reader(request))

    @app.get("/api/devices")
    async def devices(request: Request):
        reader(request)
        return ap.devices()

    @app.get("/api/order/{order_id}")
    async def order(order_id: str, request: Request):
        reader(request)
        return ap.order_get(order_id) or JSONResponse({"error": "not found"}, status_code=404)

    # -- portal login (fleet.md R84) ---------------------------------------------------------

    @app.post("/api/login/begin")
    async def login_begin(request: Request):
        b = await body(request)
        return ap.login_begin(str(b.get("site", "")), str(b.get("node", "")),
                              str(b.get("return", "")))

    @app.post("/api/login/finish")
    async def login_finish(request: Request):
        b = await body(request)
        return ap.login_finish(b.get("nonce", ""), b.get("assertion") or {})

    return app

"""Populate the dev-harness sandbox via the live web-ui backend.

Hits the running uvicorn over HTTP on loopback (no auth). No ``awm.*``
imports, no service-level shortcuts — the seeder exercises the exact
endpoints the UI uses.

Idempotent: projects and scopes return 409 if they already exist; room
seeding is skipped if any active room is present. Safe to re-run.

Run via ``dev/run.sh seed``.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


HERE = Path(__file__).resolve().parent

HOST = "127.0.0.1"
PORT = int(os.environ.get("AWM_PORT", "7821"))
BASE = f"http://{HOST}:{PORT}"


def _assert_sandbox() -> None:
    ws = os.environ.get("AWM_WORKSPACE")
    if not ws or Path(ws).resolve() != HERE:
        raise SystemExit(
            f"refusing to seed: AWM_WORKSPACE must point at {HERE} (got {ws!r}). "
            f"Run via dev/run.sh."
        )


def _request(method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8")
            return resp.status, (json.loads(text) if text else "")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(text)
        except json.JSONDecodeError:
            return exc.code, text


def _create(path: str, body: dict, label: str) -> None:
    code, payload = _request("POST", path, body)
    if code in (200, 201):
        print(f"[seed] created {label}")
    elif code == 409:
        print(f"[seed] {label} already exists, skipping")
    else:
        raise SystemExit(f"[seed] {label}: {code} {payload}")


def _invoke(tool: str, args: dict) -> dict:
    """Call an MCP tool via /invoke. Returns the parsed `result` payload
    (handle_tool returns JSON strings, so we parse twice)."""
    code, payload = _request("POST", "/invoke", {"name": tool, "args": args})
    if code != 200:
        raise SystemExit(f"[seed] invoke {tool}: {code} {payload}")
    inner = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(inner, str):
        try:
            return json.loads(inner)
        except json.JSONDecodeError:
            return {"_raw": inner}
    return inner or {}


PROJECTS = ["demo", "playground"]
SCOPES = [
    ("demo", "alpha", "First scope. Use the UI to browse history/artifacts."),
    ("demo", "beta", "Second scope under demo."),
    ("playground", "experiment", "Sandbox scope for UI experiments."),
]


def main() -> int:
    _assert_sandbox()

    for name in PROJECTS:
        _create("/projects", {"name": name}, f"project {name!r}")

    for project, scope, ctx in SCOPES:
        _create("/scopes",
                {"project": project, "scope": scope, "context": ctx},
                f"scope {project}/{scope}")

    # Rooms have no REST surface; use the MCP tool dispatcher. Only seed
    # the demo room if no active rooms exist yet.
    code, rooms = _request("GET", "/scopes")
    if code != 200:
        raise SystemExit(f"[seed] /scopes probe failed: {code} {rooms}")

    existing = _invoke("room_list", {"status": "active", "limit": 1})
    if existing.get("total", 0) == 0:
        room = _invoke("room_create", {
            "topic": "dev harness demo room",
            "scopes": ["demo/alpha"],
            "author": "user:dev",
        })
        room_id = room.get("id") or room.get("room", {}).get("id")
        if not room_id:
            raise SystemExit(f"[seed] room_create returned no id: {room}")
        for body in (
            "hello from the seeder — this room is fake",
            "open the Rooms tab in the UI to see this",
        ):
            _invoke("room_post", {
                "room_id": room_id, "body": body, "author": "user:dev",
            })
        print(f"[seed] created room {room_id}")
    else:
        print("[seed] active room(s) already exist, skipping room seed")

    print("[seed] done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

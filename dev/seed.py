"""Populate the dev-harness sandbox via the running modular gateway.

Hits the running gateway over HTTP on loopback (no auth). No ``awm.*``
imports, no service-level shortcuts — everything goes through the gateway's
generic ``POST /invoke {name, args}`` RPC surface, calling service-prefixed
tools (``scopes_project_create``, ``scopes_scope_create``,
``scopes_scope_post``/``scopes_scope_fetch``). Requires the ``scopes``
feature service to be registered (``dev/run.sh start`` waits for that).

Idempotent: project/scope creation returns 409 (FileExistsError) when the
entity already exists and is treated as a skip; channel seeding is skipped
if demo/alpha already has posts. Safe to re-run.

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


def _create(tool: str, args: dict, label: str) -> None:
    """Create an entity via /invoke, idempotently. The gateway maps the
    handlers' FileExistsError to HTTP 409, which we treat as "already there"."""
    code, payload = _request("POST", "/invoke", {"name": tool, "args": args})
    if code == 200:
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
        _create("scopes_project_create", {"name": name}, f"project {name!r}")

    for project, scope, ctx in SCOPES:
        _create("scopes_scope_create",
                {"project": project, "scope": scope, "context": ctx},
                f"scope {project}/{scope}")

    # A scope IS the channel — seed a couple of demo posts on demo/alpha's
    # channel via the scope-channel tools (no separate rooms anymore).
    existing = _invoke("scopes_scope_fetch", {"project": "demo", "scope": "alpha", "limit": 1})
    if existing.get("total", 0) == 0:
        for body in (
            "hello from the seeder — this is a fake channel post",
            "subscribe to demo/alpha in the UI to see this",
        ):
            _invoke("scopes_scope_post", {
                "project": "demo", "scope": "alpha",
                "author": "user:dev", "body": body, "kind": "message",
            })
        print("[seed] seeded demo/alpha channel")
    else:
        print("[seed] demo/alpha channel already has posts, skipping")

    print("[seed] done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

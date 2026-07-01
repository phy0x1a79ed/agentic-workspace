from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any

from awm.gatewayclient import ServiceAdapter

from awm.hpcllm import dao
from awm.hpcllm.dao import HpcllmDAO
from awm.hpcllm.lifecycle import ServeLifecycle
from awm.hpcllm.models import list_models, resolve_model

log = logging.getLogger("awm.hpcllm.hub_adapter")

_adapter: ServiceAdapter | None = None
_lifecycle: ServeLifecycle | None = None
_dao = HpcllmDAO()


def _make_serve_id() -> str:
    ts = int(time.time())
    rand = secrets.token_hex(4)
    return f"hpcllm_{ts}_{rand}"


API_MANIFEST: dict[str, Any] = {
    "functions": [
        {
            "name": "serve_request",
            "tool": "hpcllm_serve_request",
            "description": (
                "Request an LLM server on an HPC cluster. Returns immediately "
                "with a serve_id; subscribe to serve.status for lifecycle "
                "updates. When status becomes 'ready', the API is available "
                "at the api_url included in the emission."
            ),
            "params": [
                {"name": "model", "type": "string", "required": True},
                {"name": "cluster", "type": "string", "required": False},
                {"name": "hours", "type": "number", "required": False},
                {"name": "gpus", "type": "number", "required": False},
                {"name": "cpus", "type": "number", "required": False},
                {"name": "mem", "type": "number", "required": False},
            ],
        },
        {
            "name": "serve_status",
            "tool": "hpcllm_serve_status",
            "description": "Get current status of a serve request.",
            "params": [
                {"name": "serve_id", "type": "string", "required": True},
            ],
        },
        {
            "name": "serve_cancel",
            "tool": "hpcllm_serve_cancel",
            "description": (
                "Cancel a serve request: scancel the SLURM job, remove the "
                "SSH tunnel, and clean up scratch files. Idempotent."
            ),
            "params": [
                {"name": "serve_id", "type": "string", "required": True},
            ],
        },
        {
            "name": "serve_list",
            "tool": "hpcllm_serve_list",
            "description": "List all active (non-terminal) serve requests.",
            "params": [],
        },
        {
            "name": "model_list",
            "tool": "hpcllm_model_list",
            "description": "List available models for a cluster (or all).",
            "params": [
                {"name": "cluster", "type": "string", "required": False},
            ],
        },
    ],
    "emitters": [
        {
            "topic": "serve.status",
            "description": (
                "Fires on each serve lifecycle transition. Payload includes "
                "{serve_id, status, model, cluster, timestamp, message}. "
                "On status 'ready', additionally includes api_url and node. "
                "Status progression: staging -> submitted -> queued -> "
                "running -> loading -> ready -> cancelled/error."
            ),
        },
    ],
    "sessions": [],
}


async def _serve_request(args: dict) -> dict:
    model_name = str(args["model"]).strip()
    cluster = str(args.get("cluster", "sockeye")).strip()
    hours = int(args.get("hours", 4))
    gpus = int(args.get("gpus", 1))
    cpus = int(args.get("cpus", 4))
    mem = int(args.get("mem", 32))

    if resolve_model(model_name, cluster) is None:
        available = [
            m["name"] for m in list_models(cluster)
            if cluster in m.get("clusters", [])
        ]
        return {
            "error": f"model {model_name!r} not available on {cluster}; "
                     f"available: {available}",
        }

    serve_id = _make_serve_id()
    _dao.create(serve_id, model_name, cluster)

    await _lifecycle.start(serve_id, model_name, cluster,
                           hours, gpus, cpus, mem)

    return {"serve_id": serve_id, "model": model_name, "cluster": cluster}


async def _serve_status(args: dict) -> dict:
    serve_id = str(args["serve_id"]).strip()
    rec = _dao.get(serve_id)
    if rec is None:
        return {"error": f"serve_id {serve_id!r} not found"}
    return {
        "serve_id": rec["serve_id"],
        "status": rec["status"],
        "model": rec["model"],
        "cluster": rec["cluster"],
        "slurm_job_id": rec["slurm_job_id"] or None,
        "node": rec["node"] or None,
        "local_port": rec["local_port"] or None,
        "error_message": rec["error_message"] or None,
        "created_at": rec["created_at"],
        "updated_at": rec["updated_at"],
    }


async def _serve_cancel(args: dict) -> dict:
    serve_id = str(args["serve_id"]).strip()
    await _lifecycle.cancel(serve_id)
    return {"serve_id": serve_id, "status": "cancelled"}


async def _serve_list(args: dict) -> dict:
    records = _dao.list_active()
    return {"serves": records}


async def _model_list(args: dict) -> dict:
    cluster = args.get("cluster")
    if cluster is not None:
        cluster = str(cluster).strip()
    return {"models": list_models(cluster)}


HANDLERS = {
    "serve_request": _serve_request,
    "serve_status": _serve_status,
    "serve_cancel": _serve_cancel,
    "serve_list": _serve_list,
    "model_list": _model_list,
}


async def _emit(payload: dict) -> None:
    if _adapter is not None:
        await _adapter.emit("serve.status", payload)


async def main() -> None:
    global _adapter, _lifecycle
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    dao.init()
    _lifecycle = ServeLifecycle(_dao, _emit)
    _adapter = ServiceAdapter(
        "hpcllm", API_MANIFEST, HANDLERS, on_start=dao.init,
    )
    await _adapter.run()


if __name__ == "__main__":
    asyncio.run(main())

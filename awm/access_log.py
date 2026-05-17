"""JSON-line audit trail for the exposed listener.

One line per authenticated request that mutated state. Bodies are not
logged — the prompt of an agent_spawn or the contents of a message stay out
of the access log to avoid leaking sensitive data on disk.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from awm import config


_lock = threading.Lock()


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(
    *,
    ip: str,
    method: str,
    path: str,
    status_code: int,
    latency_ms: float,
    session_id: int | None = None,
) -> None:
    line = json.dumps(
        {
            "ts": _ts(),
            "ip": ip,
            "method": method,
            "path": path,
            "status": status_code,
            "latency_ms": round(latency_ms, 2),
            "session_id": session_id,
        },
        separators=(",", ":"),
    )
    log_path: Path = config.ACCESS_LOG
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with log_path.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")
    except OSError:
        # Don't fail the request if the access log is unwritable.
        pass

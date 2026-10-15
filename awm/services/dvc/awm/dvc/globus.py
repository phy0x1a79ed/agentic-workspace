"""Thin wrapper around the ``globus`` CLI's raw Transfer API surface.

Submission goes through ``globus api transfer post /transfer`` rather than the
``globus transfer`` subcommand, because the subcommand cannot express a
multi-item transfer document with per-item recursion flags — which is what
:mod:`awm.dvc.transfer` needs to name tens of thousands of individual cache
objects in one task.

**Submit, don't block.** A Globus task takes seconds to start and minutes-to-
hours to finish. Handlers here return the ``task_id`` immediately and leave
polling to the ``task`` verb, so no RPC is ever holding a socket open across an
86 GB transfer. This is also the reason chinook cannot simply be registered as a
DVC remote: DVC's remote contract is per-file ``exists``/``get_file``/
``put_file``, and a per-object async batch task is unusable at that granularity.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from awm.dvc.config import ChinookConfig

log = logging.getLogger("awm.dvc.globus")

# Terminal Globus task states. ACTIVE is not here on purpose: an ACTIVE task with
# faults is still retrying and may well succeed, so it is not an outcome yet.
TERMINAL = {"SUCCEEDED", "FAILED"}


class GlobusError(Exception):
    """Raised when the globus CLI exits non-zero."""


def _run(cfg: ChinookConfig, args: list[str], body: dict | None = None) -> str:
    cmd = [cfg.globus_bin, *args]
    kwargs: dict[str, Any] = {"capture_output": True, "text": True}
    if body is not None:
        kwargs["input"] = json.dumps(body)
        cmd += ["--body-file", "-"]
    proc = subprocess.run(cmd, **kwargs)
    if proc.returncode != 0:
        raise GlobusError(f"globus {' '.join(args)} failed:\n{proc.stderr.strip()}")
    return proc.stdout.strip()


def submit(
    cfg: ChinookConfig,
    items: list[dict],
    *,
    source: str,
    destination: str,
    label: str,
    sync_level: int = 1,
    delete_destination_extra: bool = False,
    skip_source_errors: bool = False,
) -> str:
    """Submit a transfer document and return its task id.

    The submission id is fetched per call and never reused — it is Globus's
    idempotency token, and replaying one silently returns the *original* task
    rather than submitting the new document.
    """
    submission_id = _run(
        cfg,
        ["api", "transfer", "get", "/submission_id", "--jmespath", "value", "--format", "unix"],
    )
    body = {
        "DATA_TYPE": "transfer",
        "submission_id": submission_id,
        "source_endpoint": source,
        "destination_endpoint": destination,
        "label": label[:128],
        "sync_level": sync_level,
        "verify_checksum": True,
        "delete_destination_extra": delete_destination_extra,
        "skip_source_errors": skip_source_errors,
        "DATA": items,
    }
    task_id = _run(
        cfg,
        ["api", "transfer", "post", "/transfer", "--jmespath", "task_id", "--format", "unix"],
        body=body,
    )
    log.info("submitted %s (%d items, label=%r)", task_id, len(items), label)
    return task_id


def skipped_errors(cfg: ChinookConfig, task_id: str) -> list[dict]:
    """Paths a task passed over because it could not read their source.

    ``skip_source_errors`` is what stops one file vanishing mid-scan from
    failing a 100k-file backup. It is *also* what lets a backup whose source was
    entirely unreadable finish **SUCCEEDED with zero files moved** — and nothing
    in the task document says so: status is a success, every counter stays at 0,
    ``files_skipped`` included. This list is the only place it shows. A
    successful run is therefore only actually successful once this is empty.
    """
    out = _run(cfg, ["task", "show", "--skipped-errors", task_id, "--format", "json"])
    return json.loads(out).get("DATA", []) if out else []


def task_status(cfg: ChinookConfig, task_id: str) -> dict:
    """Current state of a task, as the fields that actually tell you anything."""
    fields = [
        "status",
        "label",
        "files",
        "files_transferred",
        "files_skipped",
        "bytes_transferred",
        "effective_bytes_per_second",
        "faults",
        "nice_status",
        "request_time",
        "completion_time",
    ]
    raw = _run(
        cfg,
        [
            "task",
            "show",
            task_id,
            "--format",
            "unix",
            "--jmespath",
            "[" + ",".join(fields) + "]",
        ],
    )
    values = raw.split("\t")
    out: dict[str, Any] = {"task_id": task_id}
    for name, val in zip(fields, values):
        out[name] = None if val in ("None", "") else val
    for name in ("files", "files_transferred", "files_skipped", "bytes_transferred",
                 "effective_bytes_per_second", "faults"):
        if out.get(name) is not None:
            try:
                out[name] = int(out[name])
            except ValueError:
                pass
    out["done"] = out.get("status") in TERMINAL
    return out


def wait(cfg: ChinookConfig, task_id: str, timeout: int, poll: int = 15) -> dict:
    """Block until the task reaches a terminal state or ``timeout`` seconds pass.

    Returns the final status either way — a timeout is reported as
    ``done=False``, not raised, because "still running" is a normal answer for a
    multi-hour mirror and the caller can simply ask again.
    """
    proc = subprocess.run(
        [cfg.globus_bin, "task", "wait", task_id,
         "--polling-interval", str(poll), "--timeout", str(timeout)],
        capture_output=True,
        text=True,
    )
    status = task_status(cfg, task_id)
    status["timed_out"] = not status["done"] and proc.returncode != 0
    return status

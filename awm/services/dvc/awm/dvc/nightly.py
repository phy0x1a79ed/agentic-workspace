"""Entry point for the scheduled nightly cache sync — ``awm-dvc-sync``.

WHY THIS EXISTS RATHER THAN A CLI INVOCATION
The service's own verbs deliberately submit and return a task id instead of
blocking, because a Globus task runs for hours and no RPC should hold a socket
open across one. That is right for an agent, and wrong for a *scheduled job*: a
``oneshot`` systemd unit that merely submits exits 0 immediately and reports
success even when the transfer later fails — strictly worse than the shell
script it replaces, which waited and exited non-zero.

Waiting through the gateway is not available either: the generated service CLI
dispatches ``POST /invoke`` with a hard 600 s client ceiling, so no ``--wait``
long enough to cover a multi-hour transfer can survive it, whatever the
function's declared timeout says.

So the scheduled path runs the same modules **in its own process**, with no
gateway in the loop, and blocks to a terminal state. It shares
:mod:`awm.dvc.sync`'s in-flight state file, so it and the service verb still
decline to stack full-cache scans on each other.

Installed into the target env's ``bin/`` as a console script — deliberately not
a file in the workspace checkout, which is a deploy target that gets
``reset --hard`` and has already eaten one backup script whole.
"""

from __future__ import annotations

import argparse
import logging
import sys

log = logging.getLogger("awm.dvc.nightly")

# A day is the cadence, so a day is the ceiling — past that, the next tick's
# in-flight guard is a better mechanism than a longer wait here.
DEFAULT_TIMEOUT = 86400


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="awm-dvc-sync",
        description="Sync the shared DVC cache to chinook and wait for it to finish.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"seconds to wait for a terminal state (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="submit even if a previous sync task is still running",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build the transfer document, report it, submit nothing",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s %(message)s", stream=sys.stderr
    )

    from awm.dvc import sync as syncmod
    from awm.dvc.config import DvcConfigError, load

    try:
        cfg = load()
    except DvcConfigError as exc:
        log.error("%s", exc)
        return 1

    try:
        result = syncmod.sync(dry_run=args.dry_run, force=args.force)
    except Exception as exc:  # noqa: BLE001 — the exit code is the contract here
        log.error("sync failed to submit: %s", exc)
        return 1

    if args.dry_run:
        log.info("dry run: %s -> %s", result["source"], result["destination"])
        return 0

    # The in-flight guard declining to stack a second scan is the guard working,
    # not a failure — a slow run overlapping the next tick must not page anyone.
    if not result.get("submitted"):
        log.warning("%s", result.get("note", "not submitted"))
        return 0

    task_id = result["task_id"]
    log.info(
        "submitted %s (%s -> %s) — waiting up to %ds",
        task_id,
        result["source"],
        result["destination"],
        args.timeout,
    )

    from awm.dvc.globus import wait

    status = wait(cfg, task_id, timeout=args.timeout)
    log.info(
        "task %s: status=%s files=%s/%s bytes=%s faults=%s",
        task_id,
        status.get("status"),
        status.get("files_transferred"),
        status.get("files"),
        status.get("bytes_transferred"),
        status.get("faults"),
    )

    if status.get("status") == "SUCCEEDED":
        return 0
    if status.get("timed_out"):
        log.error("task %s still running after %ds", task_id, args.timeout)
    else:
        log.error("task %s ended %s: %s", task_id, status.get("status"),
                  status.get("nice_status"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

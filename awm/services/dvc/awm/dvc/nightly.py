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

So this runs the same modules **in its own process**, with no gateway in the
loop, and blocks to a terminal state. It goes through :mod:`awm.dvc.jobs` like
every other entry point, so it takes the same single-flight slot, lands in the
same run history, and cannot stack a second full-cache scan on the service's.

SINCE THE SCHEDULE MOVED INTO THE SERVICE, THIS IS AN ESCAPE HATCH
The nightly cadence is now the in-service scheduler. This stays as the one
command that backs the cache up with the gateway down or broken — the mitigation
for the independence a systemd timer had and an in-process loop does not. A run
that outlives ``--timeout`` is left live on purpose: the service's adopt sweep
picks it up and finishes recording it.

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
        "--job",
        default="cache_sync",
        help="which backup to run (default: cache_sync)",
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

    from awm.dvc import jobs, runs
    from awm.dvc.config import DvcConfigError, load

    try:
        cfg = load()
    except DvcConfigError as exc:
        log.error("%s", exc)
        return 1

    try:
        result = jobs.run_job(
            args.job, trigger="manual", dry_run=args.dry_run, force=args.force
        )
    except Exception as exc:  # noqa: BLE001 — the exit code is the contract here
        log.error("%s failed to submit: %s", args.job, exc)
        return 1

    if args.dry_run:
        log.info("dry run: %s -> %s", result.get("source", args.job),
                 result["destination"])
        return 0

    # The in-flight guard declining to stack a second scan is the guard working,
    # not a failure — a slow run overlapping the next tick must not page anyone.
    if not result.get("submitted"):
        log.warning("%s", result.get("note", "not submitted"))
        return 0

    task_id = result["task_id"]
    log.info(
        "submitted %s (-> %s) — waiting up to %ds",
        task_id,
        result["destination"],
        args.timeout,
    )

    from awm.dvc import globus as globus_mod
    from awm.dvc.globus import wait

    status = wait(cfg, task_id, timeout=args.timeout)
    # Recorded before anything is logged or returned: this process may be about
    # to be killed, and the row is the only thing that outlives it.
    jobs.record_status(runs.RunsDAO(), result["run_id"], status)

    # SUCCEEDED with everything skipped is what a totally unreadable source
    # looks like — the counters stay at zero and nothing else says so.
    try:
        skipped = globus_mod.skipped_errors(cfg, task_id)
    except Exception as exc:  # noqa: BLE001
        skipped = []
        log.warning("could not read skipped errors: %s", exc)
    if skipped:
        runs.RunsDAO().set_note(
            result["run_id"],
            f"{len(skipped)} source path(s) skipped; first: "
            f"{skipped[0].get('source_path', '')}",
        )
        log.warning("%d source path(s) skipped, first %s (%s)", len(skipped),
                    skipped[0].get("source_path"), skipped[0].get("error_code"))

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
        # Skips are normal — a live tree always races something. Skips with
        # nothing transferred are not: that is a backup that did not happen.
        if skipped and not int(status.get("files_transferred") or 0):
            log.error("task %s transferred nothing and skipped %d path(s)",
                      task_id, len(skipped))
            return 1
        return 0
    if status.get("timed_out"):
        log.error(
            "task %s still running after %ds — left live for the service to adopt",
            task_id, args.timeout,
        )
    else:
        log.error("task %s ended %s: %s", task_id, status.get("status"),
                  status.get("nice_status"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

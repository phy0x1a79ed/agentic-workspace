"""What this host's tether binaries are pinned to, and what it runs."""

from __future__ import annotations

from typing import Any

from awm.tether import daemon, paths


def release_report() -> dict[str, Any]:
    """The release this host is pinned to, and the one it runs.

    A developer pointing ``AWM_TETHER_BIN`` at their own build runs no fetch, so
    there is no version to compare. Otherwise an installed version behind the pin
    means the last fetch did not finish, and the fetch log's last line says why.
    """
    pinned = paths.pinned_release()
    if paths.BIN_OVERRIDE:
        return {"pinned": pinned, "source": f"AWM_TETHER_BIN={paths.BIN_OVERRIDE}"}
    installed = paths.installed_release()
    report: dict[str, Any] = {"pinned": pinned, "installed": installed,
                              "in_sync": pinned == installed}
    if not report["in_sync"]:
        tail = daemon.tail(paths.FETCH_LOG, 1)
        if tail:
            report["last_fetch"] = tail[-1]
    return report

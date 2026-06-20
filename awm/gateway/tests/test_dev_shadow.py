"""Tests for `awm dev shadow`'s service-subprocess watcher.

`_watch_pid` is the seam that closes the service-only-shadow auto-return gap: a
service overlay's lease is held by the spawned subprocess's adapter, not the
parent CLI, so the CLI must watch the subprocess and tear the stack down when it
exits (evicted via 4410 → adapter GiveUp, crashed, or stopped). The full
`dev_shadow` loop needs a live hub + real overlays (covered by the manual live
check in the plan); here we unit-test the watcher directly.

`awm.gateway.cli` is in the gateway dist, so the top-level import is intra-dist
(not the cross-dist case the per-dist runner forbids)."""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from awm.gateway.cli import _ServiceExited, _watch_pid


def test_watch_pid_raises_service_exited_and_reaps():
    """`_watch_pid` raises `_ServiceExited` (carrying the label) once the child
    exits, and `waitpid` has already reaped it — so it never lingers as a zombie."""
    proc = subprocess.Popen(["sleep", "0.2"], start_new_session=True)

    with pytest.raises(_ServiceExited) as excinfo:
        asyncio.run(asyncio.wait_for(_watch_pid("svc-x", proc.pid), timeout=5))

    assert "svc-x" in str(excinfo.value)

    # The watcher reaped the child: a follow-up waitpid finds no such child.
    with pytest.raises(ChildProcessError):
        os.waitpid(proc.pid, os.WNOHANG)

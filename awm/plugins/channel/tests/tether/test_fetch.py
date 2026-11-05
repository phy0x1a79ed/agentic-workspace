"""fetch-release.sh puts the binaries and assets where the adapter looks.

A singleton sets AWM_DIR to its own state directory, apart from any workspace,
so the script must resolve its state the way awm.config resolves AWM_DIR.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from awm.channel.tether import paths

pytestmark = [pytest.mark.unit]

SCRIPT = paths.SERVICE_DIR / "fetch-release.sh"

ASSETS = ["tether-relay-linux-x86_64", "tether", "tether.ps1",
          "tether-linux-x86_64", "tether-operator-linux-x86_64"]


@pytest.fixture
def release(tmp_path) -> Path:
    d = tmp_path / "release"
    d.mkdir()
    sums = []
    for name in ASSETS:
        (d / name).write_bytes(name.encode())
        sums.append(f"{hashlib.sha256(name.encode()).hexdigest()}  {name}")
    (d / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    return d


def _fetch(release: Path, awm_dir: Path, **env: str) -> None:
    subprocess.run(
        ["bash", str(SCRIPT)], check=True, capture_output=True,
        env={"PATH": os.environ["PATH"], "TETHER_RELEASE_DIR": str(release),
             "AWM_DIR": str(awm_dir), "AWM_WORKSPACE": "/nonexistent", **env})


def test_a_relay_lands_under_its_awm_dir(release, tmp_path):
    awm_dir = tmp_path / "var-lib-awm-channel"
    _fetch(release, awm_dir, AWM_TETHER_ROLE="relay")
    state = awm_dir / "services" / "channel" / "tether"
    assert (state / "bin" / "tether-relay").read_bytes() == b"tether-relay-linux-x86_64"
    assert (state / "assets" / "tether").is_file()
    assert (state / "assets" / "bin" / "tether-linux-x86_64").is_file()
    assert (state / "bin" / "VERSION").read_text().strip() == paths.pinned_release()


def test_an_operator_takes_the_daemon_only(release, tmp_path):
    _fetch(release, tmp_path / "awm", AWM_TETHER_ROLE="operator")
    state = tmp_path / "awm" / "services" / "channel" / "tether"
    assert (state / "bin" / "tether-operator").is_file()
    assert not (state / "assets").exists()

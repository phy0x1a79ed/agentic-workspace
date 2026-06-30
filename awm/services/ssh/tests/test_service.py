from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from awm.ssh.config import (
    KNOWN_HOSTS,
    control_path,
    resolve_host,
)


class TestConfig:
    def test_known_hosts_has_expected_entries(self) -> None:
        assert "sockeye" in KNOWN_HOSTS
        assert "fir" in KNOWN_HOSTS
        assert "chamois" in KNOWN_HOSTS
        assert "micb0" in KNOWN_HOSTS

    def test_sockeye_needs_vpn_and_cwl(self) -> None:
        cfg = resolve_host("sockeye")
        assert cfg.needs_vpn
        assert cfg.vpn_profile == "ubc"
        assert cfg.twofa_device == "cwl"
        assert cfg.user == "txyliu"

    def test_fir_needs_no_vpn_and_alliance(self) -> None:
        cfg = resolve_host("fir")
        assert not cfg.needs_vpn
        assert cfg.twofa_device == "alliance"
        assert cfg.user == "phyberos"

    def test_chamois_needs_vpn_and_cwl(self) -> None:
        cfg = resolve_host("chamois")
        assert cfg.needs_vpn
        assert cfg.vpn_profile == "ubc"
        assert cfg.twofa_device == "cwl"
        assert cfg.user == "tliu"

    def test_micb0_needs_vpn_and_cwl(self) -> None:
        cfg = resolve_host("micb0")
        assert cfg.needs_vpn
        assert cfg.vpn_profile == "ubc"
        assert cfg.twofa_device == "cwl"
        assert cfg.user == "tliu"

    def test_sockeye_variants(self) -> None:
        for name in ("sockeye1", "sockeye2", "sockeye3"):
            cfg = resolve_host(name)
            assert cfg.needs_vpn
            assert cfg.twofa_device == "cwl"

    def test_unknown_host_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown host"):
            resolve_host("nosuchhost")

    def test_control_path_format(self) -> None:
        cfg = resolve_host("sockeye")
        path = control_path(cfg)
        assert path.endswith("sockeye_22_txyliu")
        assert "live_connections" in path

"""Unit tests for the profile-aware discovery gate (service.toml × AWM_PROFILES).

Resolution precedence under test: explicit ``enabled.json`` entry > profile
intersection > enabled. A corrupt/invalid marker is marked-but-unmatched
(disabled), never fail-open.
"""

from __future__ import annotations

import pytest

from awm.gateway.hub import discovery


@pytest.fixture()
def services_tree(tmp_path, monkeypatch, awm_workspace):
    """A tmp services root with three folders: core (no marker), gated
    (gamebot profile), corrupt (unparseable marker)."""
    root = tmp_path / "services"
    for name, marker in (
        ("core-svc", None),
        ("gated-svc", 'profiles = ["gamebot"]\n'),
        ("corrupt-svc", "profiles = [not toml"),
    ):
        d = root / name
        d.mkdir(parents=True)
        (d / "run.sh").write_text("#!/bin/bash\n")
        if marker is not None:
            (d / "service.toml").write_text(marker)
    monkeypatch.setenv("AWM_SERVICES_DIR", str(root))
    monkeypatch.delenv("AWM_PROFILES", raising=False)
    return root


def _enabled_map(specs):
    return {s.name: s.enabled for s in specs}


class TestProfileGate:
    def test_unmarked_service_enabled_everywhere(self, services_tree):
        assert discovery.is_enabled("core-svc") is True
        assert _enabled_map(discovery.discover_services())["core-svc"] is True

    def test_marked_service_disabled_without_profile(self, services_tree):
        assert discovery.is_enabled("gated-svc") is False
        assert _enabled_map(discovery.discover_services())["gated-svc"] is False

    def test_marked_service_enabled_with_matching_profile(
            self, services_tree, monkeypatch):
        monkeypatch.setenv("AWM_PROFILES", "gamebot")
        assert discovery.is_enabled("gated-svc") is True
        assert _enabled_map(discovery.discover_services())["gated-svc"] is True

    def test_profile_list_and_whitespace(self, services_tree, monkeypatch):
        monkeypatch.setenv("AWM_PROFILES", " other , gamebot ")
        assert discovery.is_enabled("gated-svc") is True
        monkeypatch.setenv("AWM_PROFILES", "other")
        assert discovery.is_enabled("gated-svc") is False

    def test_corrupt_marker_is_disabled_not_fail_open(
            self, services_tree, monkeypatch):
        assert discovery.is_enabled("corrupt-svc") is False
        # Even with a profile active — a corrupt marker matches nothing.
        monkeypatch.setenv("AWM_PROFILES", "gamebot")
        assert discovery.is_enabled("corrupt-svc") is False

    def test_explicit_enable_beats_unmatched_profile(self, services_tree):
        discovery.set_enabled("gated-svc", True)
        assert discovery.is_enabled("gated-svc") is True
        assert _enabled_map(discovery.discover_services())["gated-svc"] is True

    def test_explicit_disable_beats_matching_profile(
            self, services_tree, monkeypatch):
        monkeypatch.setenv("AWM_PROFILES", "gamebot")
        discovery.set_enabled("gated-svc", False)
        assert discovery.is_enabled("gated-svc") is False

    def test_explicit_disable_beats_unmarked(self, services_tree):
        discovery.set_enabled("core-svc", False)
        assert discovery.is_enabled("core-svc") is False

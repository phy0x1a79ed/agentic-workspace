"""cch brings back the pinned jobs a host reboot ended, once per boot."""
from __future__ import annotations

import asyncio

import pytest

from awm.cch import hub_adapter


@pytest.fixture
def boot(tmp_path, monkeypatch):
    boot_id = tmp_path / "boot_id"
    monkeypatch.setattr(hub_adapter, "BOOT_ID", str(boot_id))
    monkeypatch.setattr(hub_adapter, "_boot_mark", lambda: tmp_path / "state" / "mark")
    return boot_id


def test_the_first_install_records_the_boot_and_revives_nothing(boot):
    boot.write_text("boot-1\n")
    assert hub_adapter._first_start_this_boot() is False
    assert hub_adapter._first_start_this_boot() is False


def test_a_new_boot_is_seen_once(boot):
    boot.write_text("boot-1\n")
    hub_adapter._first_start_this_boot()
    boot.write_text("boot-2\n")
    assert hub_adapter._first_start_this_boot() is True
    assert hub_adapter._first_start_this_boot() is False


def test_after_a_reboot_the_pinned_jobs_are_revived(boot, monkeypatch):
    boot.write_text("boot-1\n")
    hub_adapter._first_start_this_boot()
    boot.write_text("boot-2\n")
    asked = []

    async def ensured(action):
        asked.append(action)

    async def revived(args):
        return {"ok": True, "revived": ["abcd1234"], "live": [], "failed": []}

    monkeypatch.setattr(hub_adapter.lifecycle, "ensure_daemon", ensured)
    monkeypatch.setattr(hub_adapter, "_revive_pinned", revived)
    asyncio.run(hub_adapter._revive_after_boot())
    assert asked == ["revived"]


def test_a_restart_in_the_same_boot_revives_nothing(boot, monkeypatch):
    boot.write_text("boot-1\n")
    hub_adapter._first_start_this_boot()

    async def never(args):
        raise AssertionError("revived in the same boot")

    monkeypatch.setattr(hub_adapter, "_revive_pinned", never)
    asyncio.run(hub_adapter._revive_after_boot())

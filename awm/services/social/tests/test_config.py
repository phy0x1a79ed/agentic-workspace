"""Tests for social.toml named-account config loading."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.smoke]


def _write(path, text):
    path.write_text(text)
    return path


class TestLoad:
    def test_missing_file_returns_empty(self, tmp_path):
        from awm.social import config as cfg
        assert cfg.load(tmp_path / "absent.toml") == []

    def test_loads_named_accounts(self, tmp_path):
        from awm.social import config as cfg
        p = _write(tmp_path / "social.toml", """
[account.discord-bot]
platform = "discord"
token = "dtoken"

[account.slack-me]
platform = "slack"
token = "xoxp-utoken"
app_token = "xapp-1"
""")
        accts = {a.name: a for a in cfg.load(p)}
        assert set(accts) == {"discord-bot", "slack-me"}
        assert accts["discord-bot"].platform == "discord"
        assert accts["discord-bot"].kind == "bot"
        assert accts["slack-me"].kind == "user"  # xoxp- → user identity
        assert accts["slack-me"].app_token == "xapp-1"

    def test_bot_kind_for_xoxb(self, tmp_path):
        from awm.social import config as cfg
        p = _write(tmp_path / "social.toml", """
[account.slack-bot]
platform = "slack"
token = "xoxb-bot"
app_token = "xapp-1"
""")
        assert cfg.load(p)[0].kind == "bot"

    def test_rejects_unknown_platform(self, tmp_path):
        from awm.social import config as cfg
        p = _write(tmp_path / "social.toml", """
[account.x]
platform = "myspace"
token = "t"
""")
        with pytest.raises(cfg.SocialConfigError):
            cfg.load(p)

    def test_rejects_missing_token(self, tmp_path):
        from awm.social import config as cfg
        p = _write(tmp_path / "social.toml", """
[account.x]
platform = "slack"
""")
        with pytest.raises(cfg.SocialConfigError):
            cfg.load(p)

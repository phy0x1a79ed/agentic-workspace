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

    def test_loads_gmail_account(self, tmp_path):
        from awm.social import config as cfg
        p = _write(tmp_path / "social.toml", """
[account.gmail-me]
platform = "gmail"
token = "abcd efgh ijkl mnop"
address = "me@gmail.com"
""")
        a = cfg.load(p)[0]
        assert a.platform == "gmail"
        assert a.address == "me@gmail.com"
        assert a.kind == "user"

    def test_token_file_read_and_whitespace_stripped(self, tmp_path):
        from awm.social import config as cfg
        secret = tmp_path / "gmail.secret"
        secret.write_text("abcd efgh ijkl mnop\n")  # display spaces + newline
        p = _write(tmp_path / "social.toml", f"""
[account.gmail-me]
platform = "gmail"
address = "me@gmail.com"
token_file = "{secret}"
""")
        a = cfg.load(p)[0]
        assert a.token == "abcdefghijklmnop"  # all whitespace collapsed

    def test_token_file_relative_to_config(self, tmp_path):
        from awm.social import config as cfg
        (tmp_path / "s").mkdir()
        (tmp_path / "s" / "tok").write_text("xoxb-secret")
        p = _write(tmp_path / "social.toml", """
[account.slack-bot]
platform = "slack"
token_file = "s/tok"
""")
        assert cfg.load(p)[0].token == "xoxb-secret"

    def test_missing_token_and_token_file_rejected(self, tmp_path):
        from awm.social import config as cfg
        p = _write(tmp_path / "social.toml", """
[account.x]
platform = "discord"
""")
        with pytest.raises(cfg.SocialConfigError):
            cfg.load(p)

    def test_gmail_requires_address(self, tmp_path):
        from awm.social import config as cfg
        p = _write(tmp_path / "social.toml", """
[account.gmail-me]
platform = "gmail"
token = "abcd efgh ijkl mnop"
""")
        with pytest.raises(cfg.SocialConfigError):
            cfg.load(p)

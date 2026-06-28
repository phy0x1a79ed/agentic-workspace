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

    def test_awm_social_config_override(self, tmp_path, monkeypatch):
        from awm.social import config as cfg
        p = _write(tmp_path / "elsewhere.toml", """
[account.discord-bot]
platform = "discord"
token = "dtoken"
""")
        monkeypatch.setenv("AWM_SOCIAL_CONFIG", str(p))
        accounts = cfg.load()  # no explicit path → resolves via the override
        assert [a.name for a in accounts] == ["discord-bot"]
        assert accounts[0].token == "dtoken"

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


class TestSlackSession:
    """Slack web-session accounts: a creds_cmd pull, or an inline xoxc+cookie pair."""

    def test_creds_cmd_makes_token_optional(self, tmp_path):
        from awm.social import config as cfg
        p = _write(tmp_path / "social.toml", """
[account.slack-mira]
platform = "slack"
creds_cmd = "ssh mira /home/tony/.local/bin/awm-slack-creds"
""")
        a = cfg.load(p)[0]
        assert a.token == ""                       # fetched live, not on disk
        assert a.creds_cmd.startswith("ssh mira")
        assert a.kind == "user"                    # session = "me", not a bot

    def test_xoxc_inline_requires_cookie(self, tmp_path):
        from awm.social import config as cfg
        p = _write(tmp_path / "social.toml", """
[account.slack-me]
platform = "slack"
token = "xoxc-abc"
""")
        with pytest.raises(cfg.SocialConfigError):
            cfg.load(p)

    def test_xoxc_inline_with_cookie_ok(self, tmp_path):
        from awm.social import config as cfg
        p = _write(tmp_path / "social.toml", """
[account.slack-me]
platform = "slack"
token = "xoxc-abc"
cookie = "xoxd-xyz"
""")
        a = cfg.load(p)[0]
        assert a.token == "xoxc-abc"
        assert a.cookie == "xoxd-xyz"
        assert a.kind == "user"

    def test_cookie_file_resolved_relative_to_config(self, tmp_path):
        from awm.social import config as cfg
        (tmp_path / "ck").write_text("xoxd-fromfile\n")
        p = _write(tmp_path / "social.toml", """
[account.slack-me]
platform = "slack"
token = "xoxc-abc"
cookie_file = "ck"
""")
        assert cfg.load(p)[0].cookie == "xoxd-fromfile"

    def test_empty_creds_cmd_rejected(self, tmp_path):
        from awm.social import config as cfg
        p = _write(tmp_path / "social.toml", """
[account.slack-mira]
platform = "slack"
creds_cmd = "   "
""")
        with pytest.raises(cfg.SocialConfigError):
            cfg.load(p)


class TestMira:
    """Accounts routed through the mira API daemon (source = "mira")."""

    def test_loads_mira_slack_and_teams(self, tmp_path):
        from awm.social import config as cfg
        tok = tmp_path / "auth.token"
        tok.write_text("bearer-secret\n")
        p = _write(tmp_path / "social.toml", f"""
[mira]
url = "https://172.16.0.24:7822"
token_file = "{tok}"
verify_tls = false

[account.slack-via-mira]
platform = "slack"
source = "mira"

[account.teams]
platform = "teams"
source = "mira"
""")
        accts = {a.name: a for a in cfg.load(p)}
        assert set(accts) == {"slack-via-mira", "teams"}
        for a in accts.values():
            assert a.source == "mira"
            assert a.mira_url == "https://172.16.0.24:7822"
            assert a.mira_token == "bearer-secret"
            assert a.mira_verify_tls is False
            assert a.kind == "user"          # mira acts as the logged-in user
            assert a.token == ""             # no platform token on disk

    def test_teams_requires_mira_source(self, tmp_path):
        from awm.social import config as cfg
        p = _write(tmp_path / "social.toml", """
[mira]
url = "https://h:7822"
token = "t"

[account.teams]
platform = "teams"
""")
        with pytest.raises(cfg.SocialConfigError):
            cfg.load(p)

    def test_mira_source_requires_mira_block(self, tmp_path):
        from awm.social import config as cfg
        p = _write(tmp_path / "social.toml", """
[account.slack-via-mira]
platform = "slack"
source = "mira"
""")
        with pytest.raises(cfg.SocialConfigError):
            cfg.load(p)

    def test_bad_source_value_rejected(self, tmp_path):
        from awm.social import config as cfg
        p = _write(tmp_path / "social.toml", """
[account.x]
platform = "slack"
source = "carrier-pigeon"
""")
        with pytest.raises(cfg.SocialConfigError):
            cfg.load(p)

    def test_mira_block_requires_url_and_token(self, tmp_path):
        from awm.social import config as cfg
        p = _write(tmp_path / "social.toml", """
[mira]
url = "https://h:7822"

[account.teams]
platform = "teams"
source = "mira"
""")
        with pytest.raises(cfg.SocialConfigError):
            cfg.load(p)  # [mira].token (or token_file) is required

    def test_teams_in_known_platforms_but_not_registry(self):
        from awm.social import config as cfg
        from awm.social import connectors
        assert "teams" in cfg.KNOWN_PLATFORMS
        assert "teams" not in connectors.REGISTRY  # only reachable via mira

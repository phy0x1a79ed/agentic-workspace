"""A singleton reads its secrets from the credential systemd loads for its unit."""

from __future__ import annotations

from awm.gatewayclient import credentials


def _credential(tmp_path, text: str) -> dict[str, str]:
    (tmp_path / credentials.CREDENTIAL).write_text(text)
    return {"CREDENTIALS_DIRECTORY": str(tmp_path)}


def test_the_units_secrets_reach_the_environment(tmp_path):
    env = _credential(tmp_path, "# channel\nAWM_TETHER_ISSUE_TOKEN=abc=def\n"
                                'export OTHER="quoted"\n')
    assert credentials.load(env) == ["AWM_TETHER_ISSUE_TOKEN", "OTHER"]
    assert env["AWM_TETHER_ISSUE_TOKEN"] == "abc=def"
    assert env["OTHER"] == "quoted"


def test_the_units_own_environment_wins(tmp_path):
    env = _credential(tmp_path, "AWM_TETHER_ROLE=operator\n")
    env["AWM_TETHER_ROLE"] = "relay"
    credentials.load(env)
    assert env["AWM_TETHER_ROLE"] == "relay"


def test_no_credential_is_nothing_to_do(tmp_path):
    assert credentials.load({}) == []
    assert credentials.load({"CREDENTIALS_DIRECTORY": str(tmp_path)}) == []

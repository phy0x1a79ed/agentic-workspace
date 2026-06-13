"""Tests for awm.config.load_env_file.

A per-workspace `.awm/env` file lets the user plumb env vars into the awm
daemon that systemd doesn't forward (canonically SSH_AUTH_SOCK for
project_create's git clone — inbox #236).
"""

from __future__ import annotations

import pytest
pytestmark = [pytest.mark.unit]

from awm.config import load_env_file


def test_missing_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr("os.environ", {})
    n = load_env_file(tmp_path / "does-not-exist")
    assert n == 0


def test_basic_key_val(tmp_path, monkeypatch):
    p = tmp_path / "env"
    p.write_text("FOO=bar\nBAZ=qux\n")
    env = {}
    monkeypatch.setattr("os.environ", env)
    n = load_env_file(p)
    assert n == 2
    assert env["FOO"] == "bar"
    assert env["BAZ"] == "qux"


def test_export_prefix_tolerated(tmp_path, monkeypatch):
    p = tmp_path / "env"
    p.write_text("export FOO=bar\n  export   BAZ=qux\n")
    env = {}
    monkeypatch.setattr("os.environ", env)
    assert load_env_file(p) == 2
    assert env["FOO"] == "bar"
    assert env["BAZ"] == "qux"


def test_comments_and_blanks_skipped(tmp_path, monkeypatch):
    p = tmp_path / "env"
    p.write_text("# leading comment\n\nFOO=bar\n  \n# trailing\n")
    env = {}
    monkeypatch.setattr("os.environ", env)
    assert load_env_file(p) == 1
    assert env == {"FOO": "bar"}


def test_quoted_values_stripped(tmp_path, monkeypatch):
    p = tmp_path / "env"
    p.write_text('A="has spaces"\nB=\'single quoted\'\nC=unquoted\n')
    env = {}
    monkeypatch.setattr("os.environ", env)
    assert load_env_file(p) == 3
    assert env["A"] == "has spaces"
    assert env["B"] == "single quoted"
    assert env["C"] == "unquoted"


def test_file_overrides_existing_env(tmp_path, monkeypatch):
    p = tmp_path / "env"
    p.write_text("SSH_AUTH_SOCK=/run/user/1000/keyring/ssh\n")
    env = {"SSH_AUTH_SOCK": "/tmp/stale-from-shell"}
    monkeypatch.setattr("os.environ", env)
    load_env_file(p)
    assert env["SSH_AUTH_SOCK"] == "/run/user/1000/keyring/ssh"


def test_malformed_lines_skipped_others_applied(tmp_path, monkeypatch):
    p = tmp_path / "env"
    p.write_text("GOOD=1\nno-equals-sign\nALSO_GOOD=2\n=missing-key\n123BAD=x\n")
    env = {}
    monkeypatch.setattr("os.environ", env)
    n = load_env_file(p)
    assert n == 2
    assert env == {"GOOD": "1", "ALSO_GOOD": "2"}


def test_value_with_equals(tmp_path, monkeypatch):
    p = tmp_path / "env"
    p.write_text("DSN=postgres://u:p@host/db?x=1\n")
    env = {}
    monkeypatch.setattr("os.environ", env)
    load_env_file(p)
    assert env["DSN"] == "postgres://u:p@host/db?x=1"

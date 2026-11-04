#!/usr/bin/env bash
# A stub harness: it knows one caller token, for one untrusted isolated session.
export FAKE_TOOL=harness-cc
export FAKE_TOKENS='{"good": {"session_id": "iso-1", "claude_session_id": "x", "name": "iso", "project": "awm", "scope": "iso", "trust": "untrusted", "tools": {"hello": ["echo", "whoami"]}}}'
exec ./bin/awm-fake-plugin

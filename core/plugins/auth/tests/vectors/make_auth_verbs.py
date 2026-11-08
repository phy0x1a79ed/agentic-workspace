"""Write auth_verbs.json: the auth tool's manifest, its config contract, and the settings row
the contract stores, as the Python wrote them.

Run from the repository root while the Python still exists:
    mamba run -n awm python -I core/plugins/auth/tests/vectors/make_auth_verbs.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[4]
sys.path[:0] = [str(ROOT / "awm/services/auth")] + [
    str(ROOT / "awm/service_components" / d) for d in ("config", "persistence", "gatewayclient")]

import awm.persistence.databases as databases  # noqa: E402
from awm.auth import hub_adapter  # noqa: E402
from awm.auth.config import CONTRACT  # noqa: E402

assert Path(hub_adapter.__file__).is_relative_to(ROOT), hub_adapter.__file__


def saved_rows() -> list:
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        databases.SERVICES_DIR = Path(tmp)
        path = databases.service_db_path("auth")
        for patch in ({}, {"session_ttl_hours": 12}, {"session_ttl_hours": "6.5"}, {"max_session_days": True},
                      {"session_ttl_hours": 1.0, "unknown": "dropped"}):
            got = CONTRACT.set({"values": patch})
            conn = sqlite3.connect(path)
            (value,) = conn.execute("SELECT value FROM awm_settings WHERE key = 'service_config'").fetchone()
            conn.close()
            out.append({"patch": patch, "answer": got, "stored": value})
        databases.release_keepers()
    return out


def main() -> None:
    out = {"functions": hub_adapter.API_MANIFEST["functions"], "config": hub_adapter.API_MANIFEST["config"],
           "handlers": sorted(hub_adapter.HANDLERS), "saved": saved_rows()}
    (HERE / "auth_verbs.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

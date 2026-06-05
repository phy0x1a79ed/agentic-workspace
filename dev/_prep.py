"""One-shot prep run by ``dev/run.sh`` before uvicorn starts.

Idempotent: bootstraps the sandbox DB.

Assumes AWM_WORKSPACE points at dev/. Refuses to run otherwise so a stray
invocation never touches the real workspace.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _assert_sandbox() -> None:
    ws = os.environ.get("AWM_WORKSPACE")
    here = str(Path(__file__).resolve().parent)
    if not ws or Path(ws).resolve() != Path(here):
        raise SystemExit(
            f"refusing to prep: AWM_WORKSPACE must point at {here} (got {ws!r})"
        )


def main() -> int:
    _assert_sandbox()

    from awm.db import init_db
    from awm import config

    init_db()

    print(f"[prep] db={config.DB_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

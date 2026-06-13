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

    from awm import config

    # Modular: there is no shared state.db to init. Each feature service stands
    # up its own per-service DB under SERVICES_DIR on first use / when the hub
    # spawns it. Just ensure the services dir exists for the sandbox.
    services_dir = getattr(config, "SERVICES_DIR", config.AWM_DIR / "services")
    services_dir.mkdir(parents=True, exist_ok=True)

    print(f"[prep] services_dir={services_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

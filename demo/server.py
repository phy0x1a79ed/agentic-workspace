"""Launch shim for the voice service.

This file used to host the whole FastAPI app. It's been split:
- the app lives in `voice/service.py`
- engines + orchestrator + config live in `voice/`

This shim exists because:
1. `uvicorn server:app` from `demo/` still works (back-compat with
   `restart-tts-rvc.sh`, README run commands, systemd units).
2. demo-relative paths (static dir, last_utterance.wav) resolve the
   same way regardless of where you launch.

For per-call config switching, the dev SPA POSTs to /call/start; the
service is the contract. See `.awm/context.md` for the architecture
brief and `demo/README.md` for the operator manual.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make voice/ importable from any cwd (uvicorn often invokes with cwd=demo/).
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# demo/ must be on sys.path so the engine wrappers can `from stt import ...` etc.
_DEMO = Path(__file__).resolve().parent
if str(_DEMO) not in sys.path:
    sys.path.insert(0, str(_DEMO))

from voice.service import app  # noqa: E402,F401  (re-exported as `server.app`)

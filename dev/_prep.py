"""One-shot prep run by ``dev/run.sh`` before uvicorn starts.

Idempotent: bootstraps the sandbox DB, the loopback bearer token, and the
self-signed TLS cert.

Login URLs are minted by the *live* server via ``/auth/mint`` (challenges
live in process-local memory), so this script intentionally does not
generate them — ``dev/run.sh login`` does, after the server is up.

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
    from awm.services import auth as auth_svc
    from awm.services.network import peers as peer_svc
    from awm import config

    init_db()
    auth_svc.local_token(generate_if_missing=True)
    cert, key = auth_svc.bootstrap_tls(generate_if_missing=True)

    # Local peer identity — without this, GET /peer 404s and the Status tab
    # renders an error instead of a peer_id. The dev sandbox isn't federated
    # with anyone, so the value is cosmetic.
    if peer_svc.get_local_identity() is None:
        peer_svc.set_local_identity(
            "dev-sandbox",
            advertise_url=f"https://{config.EXPOSED_HOST}:{config.EXPOSED_PORT}",
        )

    # awm.db._seed_self_peer auto-inserts a peers-table row for the local
    # identity, but does NOT place a token file at the canonical peer-token
    # path. ping_peer treats self like any other peer and calls
    # load_peer_token → FileNotFoundError → 500. The loopback bearer (the
    # one we already use for /auth/mint and friends) is the right token for
    # talking to ourselves, so copy it into place.
    ident = peer_svc.get_local_identity()
    if ident:
        self_token = config.AWM_DIR / "peers" / f"{ident['peer_id']}.token"
        if not self_token.exists():
            self_token.parent.mkdir(parents=True, exist_ok=True)
            self_token.write_text(config.AUTH_TOKEN_FILE.read_text())
            os.chmod(self_token, 0o600)

    print(f"[prep] db={config.DB_PATH}", file=sys.stderr)
    print(f"[prep] token={config.AUTH_TOKEN_FILE}", file=sys.stderr)
    print(f"[prep] cert={cert}", file=sys.stderr)
    print(f"[prep] key={key}", file=sys.stderr)
    print(f"[prep] peer={config.PEER_FILE}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

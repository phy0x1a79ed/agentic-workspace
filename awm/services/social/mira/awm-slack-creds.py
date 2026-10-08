#!/usr/bin/env python3
"""Extract a live Slack web-session credential pair from a running client on mira.

The social service can't use a token scraped from a laptop browser — it dies when
that browser session ends. Instead Slack-web runs permanently on the mira host, in
a dedicated browser launched with ``--remote-debugging-port``. This script
attaches to that client over the Chrome
DevTools Protocol and reads the two secrets the web API needs, *live*, from the
running app:

  - the ``xoxc-`` session token, from ``localStorage['localConfig_v2']``
  - the ``d`` session cookie (``xoxd-…``), via ``Network.getAllCookies`` (CDP
    exposes HttpOnly cookies, so no keyring decryption of the on-disk Cookies db)

It prints ``{"token": …, "cookie": …, "team": …, "url": …}`` as one JSON line to
stdout and NOTHING secret to stderr. The awm host invokes it over ssh as the
connector's ``creds_cmd``; the token + cookie never touch disk on either host.

Exit codes: 0 ok; 2 no debug target found (client not up / not logged in); 3 no
token/cookie in the running session (logged out); 4 transport/protocol error.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

try:
    import websocket  # websocket-client (sync); installed in the helper venv
except ImportError:  # pragma: no cover - venv missing the dep
    print("websocket-client not installed in the helper venv", file=sys.stderr)
    sys.exit(4)


def _targets(port: int) -> list[dict]:
    url = f"http://127.0.0.1:{port}/json"
    with urllib.request.urlopen(url, timeout=8) as r:  # noqa: S310 - localhost only
        return json.loads(r.read().decode())


def _pick_target(targets: list[dict], match: str) -> dict | None:
    """A page target whose URL/title looks like the Slack client."""
    m = match.lower()
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    for t in pages:
        if m in (t.get("url", "") + " " + t.get("title", "")).lower():
            return t
    # Fall back to any slack.com page even if `match` was more specific.
    for t in pages:
        if "slack.com" in t.get("url", "").lower():
            return t
    return None


class _CDP:
    def __init__(self, ws_url: str) -> None:
        # max_size disabled: localConfig_v2 can be large with many workspaces.
        # suppress_origin: recent Chromium/Electron reject a CDP upgrade carrying
        # a disallowed Origin header (the DNS-rebinding guard); sending none passes.
        self.ws = websocket.create_connection(
            ws_url, timeout=12, max_size=None, suppress_origin=True)
        self._id = 0

    def cmd(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:  # skip interleaved CDP events (no "id")
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def _token_for(local_config: dict, team_hint: str) -> tuple[str, str, str]:
    """(token, team_id, url) — prefer the team whose url/domain matches the hint."""
    teams = (local_config or {}).get("teams") or {}
    candidates = []
    for tid, team in teams.items():
        tok = team.get("token", "") or ""
        if tok.startswith("xoxc-"):
            candidates.append((tid, tok, team.get("url", "") or team.get("domain", "")))
    if not candidates:
        return "", "", ""
    h = team_hint.lower()
    for tid, tok, url in candidates:
        if h and h in url.lower():
            return tok, tid, url
    return candidates[0][1], candidates[0][0], candidates[0][2]


# The app.slack.com tab in the dedicated Opera (`awm-opera-teams`) is the one
# logged-in Slack session on mira. Kept as a list so another client can be added
# back without reshaping the caller.
_DEFAULT_CANDIDATES = ((9224, "app.slack.com"),)


def _extract(port: int, match: str, team: str) -> tuple[dict | None, str]:
    """Try one (port, match) target. Returns (creds | None, reason-if-none)."""
    try:
        targets = _targets(port)
    except Exception as exc:  # noqa: BLE001
        return None, f":{port} no CDP endpoint ({exc})"
    target = _pick_target(targets, match)
    if target is None:
        return None, f":{port} no Slack page target"
    cdp = _CDP(target["webSocketDebuggerUrl"])
    try:
        cdp.cmd("Network.enable")
        ev = cdp.cmd("Runtime.evaluate", {
            "expression": "localStorage.getItem('localConfig_v2')",
            "returnByValue": True,
        })
        raw = (ev.get("result") or {}).get("value")
        local_config = json.loads(raw) if raw else {}
        token, team_id, url = _token_for(local_config, team)
        cookie = ""
        for c in cdp.cmd("Network.getAllCookies").get("cookies", []):
            if c.get("name") == "d" and "slack.com" in (c.get("domain", "") or ""):
                cookie = c.get("value", "")
                break
    finally:
        cdp.close()
    if not token or not cookie:
        return None, f":{port} logged out (no {'token' if not token else 'd cookie'})"
    return {"token": token, "cookie": cookie, "team": team_id, "url": url}, ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=None,
                    help="CDP port; defaults to 9224 (the app.slack.com tab in Opera)")
    ap.add_argument("--match", default=None,
                    help="substring to pick the client's page target")
    ap.add_argument("--team", default="hallam-lab",
                    help="workspace url/domain hint to pick the token")
    args = ap.parse_args()

    if args.port is not None:
        candidates = [(args.port, args.match or "slack")]
    else:
        candidates = list(_DEFAULT_CANDIDATES)

    reasons = []
    for port, match in candidates:
        creds, why = _extract(port, match, args.team)
        if creds is not None:
            json.dump(creds, sys.stdout)
            sys.stdout.write("\n")
            return 0
        reasons.append(why)
    print("no live Slack session found: " + "; ".join(reasons), file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())

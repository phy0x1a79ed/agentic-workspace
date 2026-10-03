#!/usr/bin/env python
"""Google Drive OAuth2 consent → prints a refresh token.

Run this to mint the ``refresh_token`` that a ``[bucket.<name>]`` of
``kind = "google_drive"`` needs. It performs the interactive consent flow with an
OAuth *Desktop app* client you create once in the Google Cloud console, then
prints the resulting refresh token (and a ready TOML snippet) for you to paste
into ``social.toml``.

Nothing is written to disk by this script — the refresh token is printed so YOU
decide where it lands (inline, or a gitignored ``*_file`` referenced from the
config). The token is a long-lived secret: treat it like a password.

Prerequisites (one-time, in https://console.cloud.google.com):
  1. Create / pick a project; enable the **Google Drive API**.
  2. Configure the OAuth consent screen. **Publish it to Production.** While it
     sits in *Testing*, Google expires every refresh token after seven days, and
     the bucket dies with ``invalid_grant: Bad Request`` a week after it last
     worked — re-minting without publishing just buys another week.
  3. Create an OAuth client of type **Desktop app**.

Consent happens in a browser, and the redirect comes back to a local port. So on
a headless host, forward that port from wherever your browser is:

    # on your workstation:
    ssh -L 8765:localhost:8765 <host>
    # in that session, on the host:
    python scripts/gdrive_auth.py --port 8765 --no-browser \\
        --client-secrets /path/to/client_secret.json

It prints a URL; open it in your browser, consent, and the redirect lands back
through the tunnel. (There is no browserless "paste the code" flow any more:
Google retired the OOB redirect in 2022, and ``run_console`` went with it.)

Re-minting for a client you already have configured needs no JSON download —
pass the id and the secret file ``social.toml`` already points at:

    python scripts/gdrive_auth.py --port 8765 --no-browser \\
        --client-id <id>.apps.googleusercontent.com \\
        --client-secret-file ~/agentic_workspace/.awm/social-secrets/<name>.secret

The scope is the full ``drive`` scope — read + write + delete across the whole
Drive — because "full access" to pre-existing files needs it (``drive.file``
only ever sees files this client itself created).
"""

from __future__ import annotations

import argparse
import sys

SCOPES = ["https://www.googleapis.com/auth/drive"]


def _flow_from_args(args, InstalledAppFlow):
    """Build the flow from a downloaded client JSON, or from id + secret file."""
    if args.client_secrets:
        return InstalledAppFlow.from_client_secrets_file(
            args.client_secrets, scopes=SCOPES)
    secret = open(args.client_secret_file).read().strip()
    return InstalledAppFlow.from_client_config(
        {"installed": {
            "client_id": args.client_id,
            "client_secret": secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }},
        scopes=SCOPES)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--client-secrets",
        help="path to the Desktop-app OAuth client JSON downloaded from GCP")
    ap.add_argument(
        "--client-id",
        help="OAuth client id, when re-minting for an already-configured client")
    ap.add_argument(
        "--client-secret-file",
        help="file holding that client's secret (pairs with --client-id)")
    ap.add_argument(
        "--no-browser", action="store_true",
        help="do not try to open a browser here; just print the consent URL. "
             "Pair with --port and an ssh tunnel from a machine that has one.")
    ap.add_argument(
        "--port", type=int, default=0,
        help="local port to receive the OAuth redirect (default: any free port). "
             "Pin it when tunnelling — the tunnel needs a port known in advance.")
    ap.add_argument(
        "--bucket-name", default="gdrive-me",
        help="name to use in the printed TOML snippet (default: gdrive-me)")
    args = ap.parse_args()

    if not args.client_secrets and not (args.client_id and args.client_secret_file):
        ap.error("give --client-secrets, or both --client-id and "
                 "--client-secret-file")
    if args.no_browser and not args.port:
        ap.error("--no-browser needs an explicit --port: the redirect has to "
                 "reach a port your ssh tunnel forwards")

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("google-auth-oauthlib is not installed in this env:\n"
              "    pip install google-auth-oauthlib google-api-python-client "
              "google-auth", file=sys.stderr)
        return 2

    flow = _flow_from_args(args, InstalledAppFlow)

    # access_type=offline + prompt=consent guarantees a refresh_token is issued
    # (Google omits it on a repeat consent unless prompt=consent is forced).
    creds = flow.run_local_server(
        port=args.port,
        open_browser=not args.no_browser,
        authorization_prompt_message="Open this URL in a browser:\n\n{url}\n",
        access_type="offline", prompt="consent")

    if not creds.refresh_token:
        print("No refresh token was returned — re-run with a fresh consent "
              "(revoke prior access at https://myaccount.google.com/permissions "
              "then retry).", file=sys.stderr)
        return 1

    client_id = flow.client_config["client_id"]
    client_secret = flow.client_config["client_secret"]

    print("\n=== Google Drive refresh token ===")
    print(creds.refresh_token)
    print("\n=== paste into social.toml (secrets prefer *_file refs) ===")
    print(f"[bucket.{args.bucket_name}]")
    print('kind = "google_drive"')
    print(f'client_id = "{client_id}"')
    print(f'client_secret = "{client_secret}"')
    print(f'refresh_token = "{creds.refresh_token}"')
    print("# root = \"<drive-folder-id>\"   # optional: scope to one folder")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

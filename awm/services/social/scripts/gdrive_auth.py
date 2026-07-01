#!/usr/bin/env python
"""One-time Google Drive OAuth2 consent → prints a refresh token.

Run this **once per Google account** to mint the ``refresh_token`` that a
``[bucket.<name>]`` of ``kind = "google_drive"`` needs. It performs the
interactive consent flow with an OAuth *Desktop app* client you create once in
the Google Cloud console, then prints the resulting refresh token (and a ready
TOML snippet) for you to paste into ``social.toml``.

Nothing is written to disk by this script — the refresh token is printed so YOU
decide where it lands (inline, or a gitignored ``*_file`` referenced from the
config). The token is a long-lived secret: treat it like a password.

Prerequisites (one-time, in https://console.cloud.google.com):
  1. Create / pick a project; enable the **Google Drive API**.
  2. Configure the OAuth consent screen (External; add your Google account as a
     test user so it can consent without app verification).
  3. Create an OAuth client of type **Desktop app**; download its JSON.

Usage:
    python scripts/gdrive_auth.py --client-secrets /path/to/client_secret.json
    # then visit the printed URL (or it opens a browser), consent, and copy the
    # refresh token it prints.

The scope is the full ``drive`` scope — read + write + delete across the whole
Drive — because "full access" to pre-existing files needs it (``drive.file``
only ever sees files this client itself created).
"""

from __future__ import annotations

import argparse
import sys

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--client-secrets", required=True,
        help="path to the Desktop-app OAuth client JSON downloaded from GCP")
    ap.add_argument(
        "--no-browser", action="store_true",
        help="use the console flow (print a URL) instead of a local server")
    ap.add_argument(
        "--bucket-name", default="gdrive-me",
        help="name to use in the printed TOML snippet (default: gdrive-me)")
    args = ap.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("google-auth-oauthlib is not installed in this env:\n"
              "    pip install google-auth-oauthlib google-api-python-client "
              "google-auth", file=sys.stderr)
        return 2

    flow = InstalledAppFlow.from_client_secrets_file(
        args.client_secrets, scopes=SCOPES)

    # access_type=offline + prompt=consent guarantees a refresh_token is issued
    # (Google omits it on a repeat consent unless prompt=consent is forced).
    if args.no_browser:
        creds = flow.run_console(
            access_type="offline", prompt="consent")
    else:
        creds = flow.run_local_server(
            port=0, access_type="offline", prompt="consent")

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

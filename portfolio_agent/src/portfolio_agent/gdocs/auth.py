"""
Per-user OAuth2 auth for Google Docs + Drive.

Model B: each user's token JSON is stored in portfolio_agent.user_google_config.
The server holds only the OAuth app registration (client_secret.json).

Usage:
  # One-time interactive flow (CLI: portfolio-agent auth-user --email ...)
  creds = run_oauth_flow(client_secrets_path)
  token_json = creds.to_json()  # persist to DB

  # Every subsequent run inside the pipeline
  creds = build_creds_for_user(token_json, client_secrets_path)
  # If creds were refreshed, creds.to_json() will differ — save it back to DB.
"""
from __future__ import annotations

import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]


def build_creds_for_user(
    token_json: str,
    client_secrets_path: Path,
) -> Credentials:
    """
    Load credentials from a stored token JSON string and refresh if expired.
    Returns the (possibly refreshed) Credentials object.
    Caller should compare `.to_json()` to the original and persist if different.
    """
    creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError(
                "OAuth token is invalid and cannot be refreshed. "
                "Re-run `portfolio-agent auth-user --email <EMAIL>`."
            )
    return creds


def run_oauth_flow(client_secrets_path: Path) -> Credentials:
    """
    Interactive OAuth2 consent flow for WSL2 / headless environments.

    run_local_server handles everything: it generates the state, prints the URL
    ("Please visit this URL to authorize this application: ...") and waits for
    the callback on a random localhost port. Copy-paste the printed URL into
    any browser — on WSL2, a Windows browser accessing localhost works fine.
    """
    import sys

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets_path), SCOPES)

    print("\n" + "=" * 70, flush=True)
    print("The authorization URL will be printed on the next line.", flush=True)
    print("Copy-paste it into your browser, sign in, and click Allow.", flush=True)
    print("=" * 70 + "\n", flush=True)
    sys.stdout.flush()

    # run_local_server prints the URL internally and handles the state/callback.
    # Do NOT call authorization_url() separately — that creates a state mismatch.
    creds = flow.run_local_server(port=0, open_browser=False)
    return creds

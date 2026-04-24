"""Local helper script to obtain Gmail OAuth refresh token."""

from __future__ import annotations

import json
import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CREDENTIALS_FILE = Path(__file__).resolve().parent.parent / "client_secret_706034452884-8u5fq9rsmb33o52ltj5qp4gnv668v2gl.apps.googleusercontent.com.json"


def run_oauth_flow() -> str:
    """Run one-time local OAuth flow using the downloaded credentials JSON."""
    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(f"Credentials file not found: {CREDENTIALS_FILE}")

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_FILE),
        scopes=[GMAIL_SCOPE],
    )
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not credentials.refresh_token:
        raise RuntimeError(
            "No refresh token received. Revoke app access in Google Account and try again."
        )

    return credentials.refresh_token


def main() -> None:
    """Print refresh token for secure storage in Secret Manager."""
    token = run_oauth_flow()
    print("\n✅ Refresh token obtenido exitosamente.")
    print("Guárdalo en Firebase Secret Manager como GMAIL_REFRESH_TOKEN:\n")
    print(token)


if __name__ == "__main__":
    main()

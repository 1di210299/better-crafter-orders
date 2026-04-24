"""Diagnóstico: lista todos los emails recientes de bettercrafter1@gmail.com.

Uso (en Cloud Shell o local con env vars seteadas):
    python3 scripts/diagnose_inbox.py [días_hacia_atrás]

Default: 30 días hacia atrás.
Muestra: fecha, from, to, cc, subject, attachments.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30

    client_id = os.environ["GMAIL_CLIENT_ID"]
    client_secret = os.environ["GMAIL_CLIENT_SECRET"]
    refresh_token = os.environ["GMAIL_REFRESH_TOKEN"]

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    creds.refresh(Request())
    print("✅ Auth OK\n")

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    # Profile
    profile = service.users().getProfile(userId="me").execute()
    print(f"📧 Cuenta logueada: {profile['emailAddress']}")
    print(f"📊 Total mensajes en inbox: {profile['messagesTotal']}\n")

    # Últimos N días
    after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y/%m/%d")
    query = f"after:{after}"
    print(f"🔍 Buscando con query: {query}\n")

    msgs = service.users().messages().list(userId="me", q=query, maxResults=50).execute().get("messages", [])
    print(f"📨 Encontrados: {len(msgs)} mensajes en los últimos {days} días\n")
    print("=" * 100)

    cc_count = 0
    for i, m in enumerate(msgs[:30], 1):  # mostrar solo 30
        msg = service.users().messages().get(userId="me", id=m["id"], format="metadata",
                                              metadataHeaders=["From", "To", "Cc", "Subject", "Date"]).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        has_attachments = "✅" if any(p.get("filename") for p in msg["payload"].get("parts", [])) else "  "

        if headers.get("Cc"):
            cc_count += 1

        print(f"{i:2d}. {headers.get('Date', '?')[:25]:25s} | {has_attachments}")
        print(f"    From:    {headers.get('From', '?')}")
        print(f"    To:      {headers.get('To', '?')}")
        if headers.get("Cc"):
            print(f"    Cc:      {headers.get('Cc')}")
        print(f"    Subject: {headers.get('Subject', '?')}")
        print("-" * 100)

    print(f"\n📈 Resumen: {cc_count}/{len(msgs[:30])} mensajes tienen CC")


if __name__ == "__main__":
    main()

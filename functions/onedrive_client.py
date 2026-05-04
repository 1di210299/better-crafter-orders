"""
OneDrive client for Cloud Run.

Uses a delegated refresh token (MS_REFRESH_TOKEN) stored in Cloud Run
environment variables / Secret Manager.

The refresh token auto-renews on every call — it never expires as long
as the service runs at least once every 90 days.

Required env vars:
    MS_CLIENT_ID       – Azure app client ID
    MS_TENANT_ID       – Azure tenant ID
    MS_CLIENT_SECRET   – Azure client secret
    MS_REFRESH_TOKEN   – Delegated refresh token (from onedrive_auth.py)
    ONEDRIVE_FILE_ID   – Drive item ID of the target .docx file
    ONEDRIVE_DRIVE_ID  – Drive ID (from parentReference.driveId)
"""

import os
import json
import logging
import time
import requests

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL  = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
SCOPES     = "Files.ReadWrite offline_access"

# Hardcoded fallbacks (also loaded from .env by api.py at startup)
_DEFAULTS = {
    "MS_CLIENT_ID":      "61c787fc-4991-49b4-a14c-bd14117ebdfd",
    "MS_TENANT_ID":      "consumers",
    "MS_CLIENT_SECRET":  "",  # not required for public client refresh flow
    "MS_REFRESH_TOKEN":  "M.C561_SN1.0.U.-CvwAyXO7uoHc!ZRXXNO*PhRx6fAbVNJDsLSuxgEc3DvLpOLT!m7fqvXP54JqVMXerm8!MT6rCqfvfHxmeKBcFxiQJ!c7dMszsWo9w26CnwUcpPdSuqKuDpyDIJESfOXHwaBbXWB8FkFgosgfTzaxGTMYmUuJx!h!1S6hCX*Aj7mFjLn3JksNfvkLVPdD!nUTcbA4L!nqJZD9spsi8AQqf!XAKFoP285XFqT8jssBdZAERhAKUha*V28GN4kM*0FLkL*iRPp2vwlKtxXg7GxHYiLxhqeIx61K35FrNdLhAAUguAa58gTKfdf5x1!EGFTUHLAgTFnQbhdX*1!GNgTzjGe!HsnLLaRP3640ave1I6IW",
    "ONEDRIVE_FILE_ID":  "9F9C6569035A2B06!s8f3f59c58ae4411c9bb2622519f7ee43",
    "ONEDRIVE_DRIVE_ID": "9f9c6569035a2b06",
}


def _env(key: str) -> str:
    return os.environ.get(key) or _DEFAULTS[key]


def _get_fresh_token() -> tuple[str, str]:
    """
    Exchange the stored refresh token for a new access token.
    Returns (access_token, new_refresh_token).
    The caller is responsible for persisting the new refresh token.
    """
    tenant_id      = _env("MS_TENANT_ID")
    client_id      = _env("MS_CLIENT_ID")
    client_secret  = _env("MS_CLIENT_SECRET")
    refresh_token  = _env("MS_REFRESH_TOKEN")

    url = TOKEN_URL.format(tenant=tenant_id)
    data = {
        "grant_type":    "refresh_token",
        "client_id":     client_id,
        "refresh_token": refresh_token,
        "scope":         SCOPES,
        # No client_secret — personal MSA accounts don't require it for public client flows
    }

    r = requests.post(url, data=data, timeout=30)
    r.raise_for_status()
    token_data = r.json()

    if "access_token" not in token_data:
        raise RuntimeError(f"Token refresh failed: {token_data}")

    new_refresh = token_data.get("refresh_token", refresh_token)
    return token_data["access_token"], new_refresh


def _headers() -> dict:
    """Get auth headers with a fresh access token."""
    access_token, _new_refresh = _get_fresh_token()
    return {"Authorization": f"Bearer {access_token}"}


def download_docx() -> bytes:
    """Download the target .docx file from OneDrive and return its raw bytes."""
    drive_id = _env("ONEDRIVE_DRIVE_ID")
    file_id  = _env("ONEDRIVE_FILE_ID")

    url = f"{GRAPH_BASE}/drives/{drive_id}/items/{file_id}/content"
    r = requests.get(url, headers=_headers(), timeout=60)
    r.raise_for_status()
    logger.info(f"Downloaded docx ({len(r.content)} bytes) from OneDrive")
    return r.content


def upload_docx(content: bytes) -> None:
    """Upload (replace) the target .docx file on OneDrive with new content.
    Retries up to 5 times on 423 Locked errors (file open by someone).
    """
    drive_id = _env("ONEDRIVE_DRIVE_ID")
    file_id  = _env("ONEDRIVE_FILE_ID")
    url = f"{GRAPH_BASE}/drives/{drive_id}/items/{file_id}/content"
    headers = _headers()
    headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    for attempt in range(1, 6):
        r = requests.put(url, headers=headers, data=content, timeout=120)
        if r.status_code == 423:
            wait = attempt * 10  # 10s, 20s, 30s, 40s, 50s
            logger.warning("⏳ OneDrive file locked (423) — attempt %d/5, retrying in %ds...", attempt, wait)
            time.sleep(wait)
            headers = _headers()  # refresh token before retry
            headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            continue
        r.raise_for_status()
        logger.info("✅ Uploaded updated docx to OneDrive (attempt %d)", attempt)
        return

    raise RuntimeError("OneDrive file is locked after 5 retries — please close the file and try again")


def get_file_name() -> str:
    """Return the display name of the target file (for logging/confirmation)."""
    drive_id = _env("ONEDRIVE_DRIVE_ID")
    file_id  = _env("ONEDRIVE_FILE_ID")

    url = f"{GRAPH_BASE}/drives/{drive_id}/items/{file_id}"
    r = requests.get(url, headers=_headers(), timeout=30)
    r.raise_for_status()
    return r.json().get("name", "unknown.docx")

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


def get_drive_service(cfg: Dict[str, Any]):
    paths = cfg.get("paths", {})
    scopes = cfg.get("google_drive", {}).get("scopes", ["https://www.googleapis.com/auth/drive.readonly"])
    credentials_file = Path(paths.get("google_credentials_file", "credentials/credentials.json"))
    token_file = Path(paths.get("google_token_file", "credentials/token.json"))
    token_file.parent.mkdir(parents=True, exist_ok=True)

    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_file.exists():
                raise FileNotFoundError(f"Google OAuth credentials file not found: {credentials_file}")
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), scopes)
            creds = flow.run_console()
        token_file.write_text(creds.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=creds)

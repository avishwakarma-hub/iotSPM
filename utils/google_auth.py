from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse
from typing import Any, Dict

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


def _manual_oauth(flow: InstalledAppFlow) -> Credentials:
    """Run OAuth without opening a browser on the server.

    Newer google-auth-oauthlib releases removed ``run_console()``. This helper
    keeps the server/headless behavior explicit: print the authorization URL,
    let the operator open it manually, then accept either the returned ``code``
    value or the full redirected localhost URL containing ``?code=...``.
    """

    flow.redirect_uri = "http://localhost:8080/"
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    print("\nGoogle Drive authorization required.")
    print("Open this URL in a browser, approve access, then copy the returned code.")
    print("If the browser ends at a localhost error page, copy the full address bar URL.")
    print("\nAuthorization URL:\n")
    print(auth_url)
    print()

    response = input("Paste authorization code or full redirected URL: ").strip()
    if not response:
        raise RuntimeError("No Google OAuth authorization response provided")

    code = response
    if "code=" in response and (response.startswith("http://") or response.startswith("https://")):
        parsed = urlparse(response)
        values = parse_qs(parsed.query)
        if values.get("error"):
            raise RuntimeError(f"Google OAuth error: {values['error'][0]}")
        if not values.get("code"):
            raise RuntimeError("Could not find 'code' in redirected URL")
        code = values["code"][0]

    flow.fetch_token(code=code)
    return flow.credentials


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
            creds = _manual_oauth(flow)
        token_file.write_text(creds.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=creds)

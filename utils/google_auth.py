from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


def _normalize_scopes(scopes: Any) -> List[str]:
    if isinstance(scopes, str):
        return [scopes]
    return list(scopes or ["https://www.googleapis.com/auth/drive.readonly"])


def _token_has_required_scopes(creds: Credentials, required_scopes: List[str]) -> bool:
    """Validate scopes against the actual token payload, not only requested scopes."""

    granted_scopes = set(creds.granted_scopes or creds.scopes or [])
    return set(required_scopes).issubset(granted_scopes)


def _token_file_has_required_scopes(token_file: Path, required_scopes: List[str]) -> bool:
    """Check scopes saved in token.json before passing requested scopes to Google."""

    try:
        data = json.loads(token_file.read_text(encoding="utf-8"))
    except Exception:
        return False
    saved_scopes = data.get("scopes") or data.get("scope") or []
    if isinstance(saved_scopes, str):
        saved_scopes = saved_scopes.split()
    # Older token.json files may not include a scopes field. Treat them as stale
    # so scope changes force one manual OAuth instead of failing during upload.
    return set(required_scopes).issubset(set(saved_scopes))


class GoogleDriveScopeError(RuntimeError):
    """Raised when saved OAuth credentials do not contain required scopes."""


def _manual_oauth(flow: InstalledAppFlow) -> Credentials:
    """Run OAuth without opening a browser on the server.

    Newer google-auth-oauthlib releases removed ``run_console()``. This helper
    keeps the server/headless behavior explicit: print the authorization URL,
    let the operator open it manually, then accept either the returned ``code``
    value or the full redirected localhost URL containing ``?code=...``.
    """

    if not sys.stdin.isatty():
        raise RuntimeError(
            "Google Drive OAuth is required, but this process is non-interactive. "
            "Run `python run.py auth-drive` manually once, then retry the pipeline."
        )

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

    # Google can return extra already-granted scopes, e.g. drive.file plus
    # drive.readonly. oauthlib raises a Warning exception for that by default.
    # Relaxing this still preserves OAuth validation while allowing the token
    # to be saved and reused for future unattended runs.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow.fetch_token(code=code, include_granted_scopes=True)
    return flow.credentials


def get_drive_service(cfg: Dict[str, Any], scopes: Any = None, allow_reauth: bool = True):
    paths = cfg.get("paths", {})
    scopes = _normalize_scopes(scopes or cfg.get("google_drive", {}).get("scopes"))
    credentials_file = Path(paths.get("google_credentials_file", "credentials/credentials.json"))
    token_file = Path(paths.get("google_token_file", "credentials/token.json"))
    token_file.parent.mkdir(parents=True, exist_ok=True)

    creds = None
    if token_file.exists():
        if not _token_file_has_required_scopes(token_file, scopes):
            token_file.unlink()
            creds = None
        else:
            creds = Credentials.from_authorized_user_file(str(token_file), scopes)
            if not creds.has_scopes(scopes) or not _token_has_required_scopes(creds, scopes):
                token_file.unlink()
                creds = None
    if not creds or not creds.valid:
        if not allow_reauth:
            raise GoogleDriveScopeError(
                f"Google Drive OAuth token at {token_file} is missing required scopes: {', '.join(scopes)}"
            )
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                # Scope changes (for example enabling report upload and moving
                # from drive.readonly to drive.file) can make an existing token
                # unusable. Re-run manual OAuth instead of failing unattended.
                if token_file.exists():
                    token_file.unlink()
                if not credentials_file.exists():
                    raise FileNotFoundError(f"Google OAuth credentials file not found: {credentials_file}")
                flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), scopes)
                creds = _manual_oauth(flow)
        else:
            if not credentials_file.exists():
                raise FileNotFoundError(f"Google OAuth credentials file not found: {credentials_file}")
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), scopes)
            creds = _manual_oauth(flow)
        token_file.write_text(creds.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=creds)

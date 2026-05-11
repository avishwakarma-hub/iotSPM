from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from googleapiclient.errors import HttpError, ResumableUploadError
from googleapiclient.http import MediaFileUpload

from utils.google_auth import GoogleDriveScopeError, get_drive_service

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"


class ReportUploadPermissionError(RuntimeError):
    """Raised when Google Drive auth lacks write/upload permissions."""


def _is_insufficient_permission_error(exc: BaseException) -> bool:
    """Return True for Google API 403 insufficient permission/scope errors."""

    status = getattr(getattr(exc, "resp", None), "status", None)
    text = str(exc).lower()
    return (
        (status == 403 and ("insufficient" in text or "insufficientpermissions" in text))
        or "insufficient authentication scopes" in text
        or "oauth is required" in text
        or "missing required scopes" in text
    )


def _upload_permission_message(cfg: Dict[str, Any]) -> str:
    token_file = cfg.get("paths", {}).get("google_token_file", "credentials/token.json")
    return (
        "Google Drive upload failed because the saved OAuth token does not have upload scope. "
        f"Delete/recreate the token at {token_file}, then run `python run.py auth-drive` manually."
    )


def upload_report_if_enabled(cfg: Dict[str, Any], report_path: str | Path) -> Optional[Dict[str, str]]:
    """Upload the final review report to Google Drive when configured.

    This is optional and independent from the Rundeck download Drive auth. It
    needs a write-capable Drive scope such as ``drive.file`` in config.
    """

    upload_cfg = cfg.get("report_upload", {})
    if not upload_cfg.get("enabled", False):
        return None

    report_path = Path(report_path)
    if not report_path.is_file():
        raise FileNotFoundError(f"Review report not found for upload: {report_path}")

    try:
        service = get_drive_service(cfg, scopes=[DRIVE_FILE_SCOPE], allow_reauth=False)
    except (GoogleDriveScopeError, RuntimeError) as exc:
        if _is_insufficient_permission_error(exc):
            raise ReportUploadPermissionError(_upload_permission_message(cfg)) from exc
        raise
    metadata: Dict[str, Any] = {"name": upload_cfg.get("filename") or report_path.name}
    folder_id = upload_cfg.get("folder_id")
    if folder_id:
        metadata["parents"] = [folder_id]

    media = MediaFileUpload(
        str(report_path),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        resumable=True,
    )
    try:
        created = (
            service.files()
            .create(body=metadata, media_body=media, fields="id, webViewLink")
            .execute()
        )
    except (HttpError, ResumableUploadError) as exc:
        if _is_insufficient_permission_error(exc):
            raise ReportUploadPermissionError(_upload_permission_message(cfg)) from exc
        raise
    return {"file_id": created.get("id", ""), "web_view_link": created.get("webViewLink", "")}

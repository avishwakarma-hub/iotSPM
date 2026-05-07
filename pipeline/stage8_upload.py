from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from googleapiclient.http import MediaFileUpload

from utils.google_auth import get_drive_service


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

    service = get_drive_service(cfg)
    metadata: Dict[str, Any] = {"name": upload_cfg.get("filename") or report_path.name}
    folder_id = upload_cfg.get("folder_id")
    if folder_id:
        metadata["parents"] = [folder_id]

    media = MediaFileUpload(
        str(report_path),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        resumable=True,
    )
    created = (
        service.files()
        .create(body=metadata, media_body=media, fields="id, webViewLink")
        .execute()
    )
    return {"file_id": created.get("id", ""), "web_view_link": created.get("webViewLink", "")}

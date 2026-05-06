from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Dict, Optional

from googleapiclient.http import MediaIoBaseDownload

from utils.google_auth import get_drive_service


def download_drive_file(cfg: Dict[str, Any], file_id: str, output_dir: Optional[str] = None) -> Path:
    service = get_drive_service(cfg)
    file_meta = service.files().get(fileId=file_id, fields="name, mimeType").execute()
    file_name = file_meta.get("name", file_id)
    out_dir = Path(output_dir or cfg["paths"]["raw_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    output_path = out_dir / file_name

    request = service.files().get_media(fileId=file_id)
    with io.FileIO(output_path, "wb") as handle:
        downloader = MediaIoBaseDownload(handle, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return output_path

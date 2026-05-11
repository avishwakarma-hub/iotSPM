from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.config import load_config  # noqa: E402
from utils.spm_kb import build_knowledge_base, save_knowledge_base  # noqa: E402


class SpmExportClient:
    def __init__(self, cfg: Dict[str, Any]):
        spm_cfg = cfg.get("spm", {})
        export_cfg = cfg.get("spm_export", {})
        self.cfg = export_cfg
        self.base_url = str(export_cfg.get("api_url") or spm_cfg.get("url") or "").rstrip("/")
        self.api_key = str(export_cfg.get("api_key") or spm_cfg.get("api_key") or "")
        self.verify_ssl = bool(export_cfg.get("verify_ssl", True))
        self.timeout = int(export_cfg.get("request_timeout_seconds") or spm_cfg.get("request_timeout_seconds") or 60)

    def headers(self) -> Dict[str, str]:
        headers = {"accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Api-Key {self.api_key}"
        return headers

    def latest_approved_export(self) -> Optional[Dict[str, Any]]:
        if not self.base_url:
            return None
        url = f"{self.base_url}/api/coverage/spm-exports/"
        params = {"state": "approved", "ordering": "-id", "limit": 1}
        response = requests.get(url, params=params, headers=self.headers(), timeout=self.timeout, verify=self.verify_ssl)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            return data[0] if data else None
        results = data.get("results") if isinstance(data, dict) else None
        if isinstance(results, list):
            return results[0] if results else None
        return data if isinstance(data, dict) and data.get("id") else None

    def export_detail(self, export_id: str | int) -> Dict[str, Any]:
        url = f"{self.base_url}/api/coverage/spm-exports/{export_id}/"
        response = requests.get(url, headers=self.headers(), timeout=self.timeout, verify=self.verify_ssl)
        response.raise_for_status()
        return response.json()

    def download_export(self, export_info: Dict[str, Any], download_dir: str | Path) -> Path:
        export_id = export_info.get("id")
        detail = self.export_detail(export_id) if export_id else export_info
        download_url = _download_url(detail)
        if not download_url:
            raise RuntimeError(f"Could not find download URL in SPM export response for export {export_id}: {detail}")
        if download_url.startswith("/"):
            download_url = f"{self.base_url}{download_url}"
        out_dir = Path(download_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = out_dir / f"spm_export_{export_id}.zip"
        with requests.get(download_url, headers=self.headers(), timeout=self.timeout, verify=self.verify_ssl, stream=True) as response:
            response.raise_for_status()
            with output_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
        return output_path


def ensure_spm_knowledge_base(
    cfg: Dict[str, Any],
    *,
    force: bool = False,
    logger: Any = None,
) -> Optional[Path]:
    export_cfg = cfg.get("spm_export", {})
    if not export_cfg.get("enabled", True):
        return None

    kb_path = Path(export_cfg.get("kb_cache_path") or Path(cfg.get("base_dir", ".")) / "data" / "spm_knowledge_base.json")
    configured_export_id = export_cfg.get("export_id", "latest")
    spm_filename = str(export_cfg.get("spm_filename") or "phrases_req_uri.spm")

    if configured_export_id != "latest" and kb_path.is_file() and not force:
        existing = _read_json(kb_path)
        if str(existing.get("export_id")) == str(configured_export_id):
            _log(logger, "info", "SPM KB is already current for export %s: %s", configured_export_id, kb_path)
            return kb_path

    client = SpmExportClient(cfg)
    latest = None
    if configured_export_id == "latest":
        latest = client.latest_approved_export()
        if not latest:
            _log(logger, "warning", "No latest approved SPM export found; keeping existing KB if present")
            return kb_path if kb_path.is_file() else None
        export_id = latest.get("id")
    else:
        export_id = configured_export_id
        latest = {"id": export_id}

    if kb_path.is_file() and not force:
        existing = _read_json(kb_path)
        if str(existing.get("export_id")) == str(export_id):
            _log(logger, "info", "SPM KB is already current for latest export %s: %s", export_id, kb_path)
            return kb_path

    exports_dir = Path(export_cfg.get("exports_dir") or cfg.get("paths", {}).get("spm_exports_dir") or Path(cfg.get("base_dir", ".")) / "data" / "spm_exports")
    zip_path = client.download_export(latest, exports_dir)
    extract_dir = exports_dir / f"export_{export_id}"
    spm_file = extract_spm_file(zip_path, extract_dir, spm_filename, password=f"{export_cfg.get('zip_password_prefix', 'export_')}{export_id}")
    kb_data = build_knowledge_base(spm_file, export_id=export_id, source_url=client.base_url)
    save_knowledge_base(kb_data, kb_path)
    _log(logger, "info", "Built SPM KB for export %s with %s signatures: %s", export_id, kb_data.get("total_meaningful"), kb_path)
    return kb_path


def build_from_local_file(cfg: Dict[str, Any], local_file: str | Path, *, export_id: str = "local") -> Path:
    export_cfg = cfg.get("spm_export", {})
    kb_path = Path(export_cfg.get("kb_cache_path") or Path(cfg.get("base_dir", ".")) / "data" / "spm_knowledge_base.json")
    kb_data = build_knowledge_base(local_file, export_id=export_id, source_url="local-file")
    return save_knowledge_base(kb_data, kb_path)


def extract_spm_file(zip_path: str | Path, extract_dir: str | Path, spm_filename: str, password: str) -> Path:
    extract_dir = Path(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    pwd = password.encode("utf-8") if password else None
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(extract_dir, pwd=pwd)
    candidates = list(extract_dir.rglob(spm_filename))
    if not candidates:
        candidates = [path for path in extract_dir.rglob("*.spm") if path.name == spm_filename or "phrases_req_uri" in path.name]
    if not candidates:
        raise FileNotFoundError(f"Could not find {spm_filename} after extracting {zip_path} to {extract_dir}")
    return candidates[0]


def _download_url(data: Dict[str, Any]) -> str:
    for key in ("download_url", "file", "url", "archive", "export_file"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            nested = _download_url(value)
            if nested:
                return nested
    return ""


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}


def _log(logger: Any, level: str, message: str, *args: Any) -> None:
    if logger:
        getattr(logger, level)(message, *args)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch latest SPM export and build local IoT SPM knowledge base")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--force", action="store_true", help="Rebuild even if cached KB export_id is already latest")
    parser.add_argument("--local-file", help="Build KB from an already extracted phrases_req_uri.spm file")
    parser.add_argument("--export-id", default=None, help="Override export id metadata or fetch a specific export id")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.local_file:
        path = build_from_local_file(cfg, args.local_file, export_id=args.export_id or "local")
    else:
        if args.export_id:
            cfg.setdefault("spm_export", {})["export_id"] = args.export_id
        path = ensure_spm_knowledge_base(cfg, force=args.force)
    print(path or "SPM KB not built")


if __name__ == "__main__":
    main()

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Dict

import yaml


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_yaml(path: str | Path) -> Dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        return {}
    with file_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_config(config_path: str | Path = "config/settings.yaml") -> Dict[str, Any]:
    config_path = Path(config_path)
    cfg = load_yaml(config_path)

    local_path = config_path.with_name("settings.local.yaml")
    if local_path.exists():
        cfg = _deep_merge(cfg, load_yaml(local_path))

    cfg = _apply_env_overrides(cfg)
    return cfg


def _apply_env_overrides(cfg: Dict[str, Any]) -> Dict[str, Any]:
    env_map = {
        "RUNDECK_USERNAME": ("rundeck", "username"),
        "RUNDECK_PASSWORD": ("rundeck", "password"),
        "RUNDECK_BASE_URL": ("rundeck", "base_url"),
        "ZINTEL_API_KEY": ("spm", "api_key"),
        "SMTP_USERNAME": ("smtp", "username"),
        "SMTP_PASSWORD": ("smtp", "password"),
        "SMTP_HOST": ("smtp", "host"),
        "ALERT_EMAIL_FROM": ("smtp", "alert_email_from"),
        "ALERT_EMAIL_TO": ("smtp", "alert_email_to"),
        "IOTSPM_BASE_DIR": ("base_dir",),
    }
    for env_name, path in env_map.items():
        value = os.getenv(env_name)
        if not value:
            continue
        target = cfg
        for key in path[:-1]:
            target = target.setdefault(key, {})
        if env_name == "ALERT_EMAIL_TO":
            target[path[-1]] = [item.strip() for item in value.split(",") if item.strip()]
        else:
            target[path[-1]] = value
    return cfg


def ensure_directories(cfg: Dict[str, Any]) -> None:
    paths = cfg.get("paths", {})
    for key, value in paths.items():
        if key.endswith("_dir") or key in {"logs_dir", "credentials_dir"}:
            Path(value).mkdir(parents=True, exist_ok=True)
    if paths.get("db_path"):
        Path(paths["db_path"]).parent.mkdir(parents=True, exist_ok=True)
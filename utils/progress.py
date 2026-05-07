from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProgressReporter:
    enabled: bool = False
    every: int = 100
    width: int = 24

    def stage(self, name: str, message: str = "") -> None:
        if not self.enabled:
            return
        suffix = f" — {message}" if message else ""
        print(f"\n[{name}] starting{suffix}", flush=True)

    def done(self, name: str, message: str = "") -> None:
        if not self.enabled:
            return
        suffix = f" — {message}" if message else ""
        print(f"[{name}] done{suffix}", flush=True)

    def info(self, name: str, message: str) -> None:
        if not self.enabled:
            return
        print(f"[{name}] {message}", flush=True)

    def update(self, name: str, current: int, total: int, message: str = "", *, force: bool = False) -> None:
        if not self.enabled or total <= 0:
            return
        if not force and current < total and current % max(1, self.every) != 0:
            return
        ratio = min(max(current / total, 0.0), 1.0)
        filled = int(self.width * ratio)
        bar = "#" * filled + "-" * (self.width - filled)
        percent = int(ratio * 100)
        suffix = f" {message}" if message else ""
        end = "\n" if current >= total else "\r"
        print(f"[{name}] [{bar}] {current}/{total} ({percent:3d}%){suffix}", end=end, flush=True)


def progress_from_config(cfg: dict) -> ProgressReporter:
    progress_cfg = cfg.get("runtime", {}).get("progress", {})
    return ProgressReporter(
        enabled=bool(progress_cfg.get("enabled", False)),
        every=int(progress_cfg.get("every", 100)),
        width=int(progress_cfg.get("width", 24)),
    )


def describe_path(path: str | Path | None) -> str:
    if not path:
        return ""
    return str(path)
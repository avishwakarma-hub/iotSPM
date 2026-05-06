from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict


def setup_logging(cfg: Dict[str, Any], name: str = "iotspm") -> logging.Logger:
    log_dir = Path(cfg.get("paths", {}).get("logs_dir", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = logging.FileHandler(log_dir / "iotspm.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger
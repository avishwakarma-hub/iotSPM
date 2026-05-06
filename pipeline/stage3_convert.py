from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict


def convert_to_csv_if_needed(cfg: Dict[str, Any], input_path: str | Path) -> Path:
    input_path = Path(input_path)
    csv_dir = Path(cfg["paths"]["csv_dir"])
    csv_dir.mkdir(parents=True, exist_ok=True)
    if input_path.suffix.lower() == ".csv":
        output_path = csv_dir / input_path.name
        if input_path.resolve() != output_path.resolve():
            output_path.write_bytes(input_path.read_bytes())
        return output_path

    output_path = csv_dir / f"{input_path.name}.csv"
    zclient = cfg.get("zclient", {}).get("binary", "/usr/local/bin/zclient")
    env = os.environ.copy()
    env["TZ"] = cfg.get("zclient", {}).get("timezone", "GMT")
    cmd = [zclient, "-o", str(output_path), "-rc", str(input_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"zclient conversion failed: {result.stderr or result.stdout}")
    return output_path

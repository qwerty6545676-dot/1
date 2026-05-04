"""JSONL writer for wall events — for offline analysis and backtesting."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class JsonlWriter:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.write("\n")

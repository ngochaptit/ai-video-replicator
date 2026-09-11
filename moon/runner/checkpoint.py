from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from moon.atomic import atomic_write_json


class CheckpointStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, stage: str) -> Path:
        return self.root / f"{stage}.json"

    def write(self, stage: str, payload: dict[str, Any]) -> Path:
        path = self.path_for(stage)
        atomic_write_json(path, payload)
        return path

    def read(self, stage: str) -> dict[str, Any]:
        return json.loads(self.path_for(stage).read_text(encoding="utf-8"))

    def exists(self, stage: str) -> bool:
        return self.path_for(stage).exists()

    def delete(self, stage: str) -> bool:
        path = self.path_for(stage)
        if not path.exists():
            return False
        path.unlink()
        return True

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class CheckpointStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, stage: str) -> Path:
        return self.root / f"{stage}.json"

    def write(self, stage: str, payload: dict[str, Any]) -> Path:
        path = self.path_for(stage)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
        return path

    def read(self, stage: str) -> dict[str, Any]:
        return json.loads(self.path_for(stage).read_text(encoding="utf-8"))

    def exists(self, stage: str) -> bool:
        return self.path_for(stage).exists()

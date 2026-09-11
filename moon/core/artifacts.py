from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from moon.atomic import atomic_write_json


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, name: str) -> Path:
        safe = name.strip().replace("\\", "_").replace("/", "_")
        if not safe:
            raise ValueError("artifact name must not be empty")
        return self.root / f"{safe}.json"

    def write(self, name: str, payload: dict[str, Any]) -> Path:
        path = self.path_for(name)
        atomic_write_json(path, payload)
        return path

    def read(self, name: str) -> dict[str, Any]:
        return json.loads(self.path_for(name).read_text(encoding="utf-8"))

    def exists(self, name: str) -> bool:
        return self.path_for(name).exists()

    def delete(self, name: str) -> bool:
        path = self.path_for(name)
        if not path.exists():
            return False
        path.unlink()
        return True

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


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
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
        return path

    def read(self, name: str) -> dict[str, Any]:
        return json.loads(self.path_for(name).read_text(encoding="utf-8"))

    def exists(self, name: str) -> bool:
        return self.path_for(name).exists()

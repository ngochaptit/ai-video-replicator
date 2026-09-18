from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from moon.atomic import atomic_write_json
from moon.core.project import MoonProject


SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class TaskStore:
    def __init__(self, project: MoonProject) -> None:
        self.root = project.moon_dir / "tasks-v2"
        self.index_path = self.root / "index.json"

    def task_dir(self, task_id: str) -> Path:
        if not SAFE_TASK_ID.fullmatch(task_id):
            raise ValueError("unsafe task_id")
        return self.root / task_id

    def read(self, task_id: str, name: str = "request.json") -> Any | None:
        path = self.task_dir(task_id) / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def write_once(self, task_id: str, name: str, payload: Any) -> Path:
        path = self.task_dir(task_id) / name
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing != payload:
                raise FileExistsError(f"immutable task artifact already exists: {path}")
            return path
        atomic_write_json(path, payload)
        return path

    def write_state(self, task_id: str, name: str, payload: Any) -> Path:
        path = self.task_dir(task_id) / name
        atomic_write_json(path, payload)
        return path

    def index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {"version": 2, "tasks": {}}
        payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), dict):
            raise ValueError("invalid tasks-v2 index")
        return payload

    def record(self, task: dict[str, Any]) -> None:
        index = self.index()
        index["tasks"][task["task_id"]] = {
            "task_id": task["task_id"],
            "signature": task["signature"],
            "stage": task["stage"],
            "revision": task["revision"],
            "project_generation": task["project_generation"],
            "parent_task_id": task.get("parent_task_id"),
        }
        atomic_write_json(self.index_path, index)

    def find_signature(self, signature: str) -> dict[str, Any] | None:
        for item in self.index().get("tasks", {}).values():
            if item.get("signature") == signature:
                return self.read(str(item["task_id"]))
        return None

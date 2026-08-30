from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_STAGES = ("proposal", "analyze", "footage", "match", "timeline", "render", "qc")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PipelineState:
    schema_version: int = 1
    stages: tuple[str, ...] = DEFAULT_STAGES
    completed: list[str] = field(default_factory=list)
    current_stage: str | None = None
    status: str = "idle"
    revision: int = 0
    updated_at: str = field(default_factory=_utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "PipelineState":
        if not path.exists():
            return cls()
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            schema_version=int(payload.get("schema_version", 1)),
            stages=tuple(payload.get("stages", DEFAULT_STAGES)),
            completed=list(payload.get("completed", [])),
            current_stage=payload.get("current_stage"),
            status=payload.get("status", "idle"),
            revision=int(payload.get("revision", 0)),
            updated_at=payload.get("updated_at", _utc_now()),
            metadata=dict(payload.get("metadata", {})),
        )

    def save(self, path: Path) -> None:
        self.updated_at = _utc_now()
        payload = {
            "schema_version": self.schema_version,
            "stages": list(self.stages),
            "completed": self.completed,
            "current_stage": self.current_stage,
            "status": self.status,
            "revision": self.revision,
            "updated_at": self.updated_at,
            "metadata": self.metadata,
        }
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)

    def next_stage(self) -> str | None:
        for stage in self.stages:
            if stage not in self.completed:
                return stage
        return None

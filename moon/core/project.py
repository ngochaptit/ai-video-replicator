from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


MOON_DIR = ".moon"


@dataclass(frozen=True)
class MoonProject:
    root: Path

    @classmethod
    def open(cls, root: str | Path, *, create: bool = False) -> "MoonProject":
        project = cls(Path(root).expanduser().resolve())
        if create:
            project.ensure_layout()
        elif not project.state_path.exists():
            raise FileNotFoundError(f"Moon project is not initialized: {project.root}")
        return project

    @property
    def moon_dir(self) -> Path:
        return self.root / MOON_DIR

    @property
    def state_path(self) -> Path:
        return self.moon_dir / "state.json"

    @property
    def project_path(self) -> Path:
        return self.moon_dir / "project.json"

    @property
    def checkpoints_dir(self) -> Path:
        return self.moon_dir / "checkpoints"

    @property
    def artifacts_dir(self) -> Path:
        return self.moon_dir / "artifacts"

    @property
    def cache_dir(self) -> Path:
        return self.moon_dir / "cache"

    @property
    def evidence_dir(self) -> Path:
        return self.moon_dir / "evidence"

    def ensure_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in (self.checkpoints_dir, self.artifacts_dir, self.cache_dir, self.evidence_dir):
            directory.mkdir(parents=True, exist_ok=True)
        if not self.project_path.exists():
            self._write_json(
                self.project_path,
                {
                    "schema_version": 1,
                    "root": str(self.root),
                    "reference": "reference.mp4",
                    "footage_dir": "footage",
                    "output_dir": "output",
                },
            )

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)

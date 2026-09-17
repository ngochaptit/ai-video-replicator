from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from moon.atomic import atomic_write_json


class MirrorTransport:
    """Traversal-safe atomic file exchange below MON_EDIT/projects/<project>."""

    def __init__(self, sync_root: Path, project_id: str) -> None:
        self.root = sync_root.expanduser().resolve() / "MON_EDIT" / "projects" / project_id

    def path(self, relative_path: str) -> Path:
        path = PurePosixPath(str(relative_path).replace("\\", "/"))
        if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError(f"unsafe mirror path: {relative_path!r}")
        resolved = self.root.joinpath(*path.parts).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"unsafe mirror path: {relative_path!r}") from exc
        return resolved

    def write_json(self, relative_path: str, payload: Any) -> Path:
        target = self.path(relative_path)
        atomic_write_json(target, payload)
        return target

    def read_json(self, relative_path: str) -> Any | None:
        path = self.path(relative_path)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def read_bytes(self, relative_path: str) -> bytes | None:
        path = self.path(relative_path)
        return path.read_bytes() if path.exists() else None

    def copy_file(self, source: Path, relative_path: str) -> Path:
        target = self.path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with source.open("rb") as source_handle, temporary.open("wb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def remove(self, relative_path: str) -> None:
        self.path(relative_path).unlink(missing_ok=True)


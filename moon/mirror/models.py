from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any


PROJECT_PROTOCOL = "mon_edit_project/2"


def normalize_project_path(value: str) -> str:
    text = str(value).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"invalid project-relative path: {value!r}")
    if ":" in path.parts[0]:
        raise ValueError(f"invalid project-relative path: {value!r}")
    return path.as_posix()


def stable_asset_id(relative_path: str) -> str:
    normalized = normalize_project_path(relative_path).casefold()
    return f"asset_{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]}"


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    role: str
    relative_path: str
    content_sha256: str
    size_bytes: int
    modified_ns: int
    proxy_path: str
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    has_video: bool = False
    has_audio: bool = False
    status: str = "ready"

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("modified_ns", None)
        return value

    def internal_dict(self) -> dict[str, Any]:
        return asdict(self)


from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from moon.core.project import MoonProject
from moon.media.probe import probe_media


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm"}


def inspect_reference(project: MoonProject) -> dict[str, Any]:
    config = _load_project_config(project)
    reference = project.root / str(config.get("reference", "reference.mp4"))
    result = probe_media(reference)
    result["role"] = "reference"
    return result


def inspect_footage(project: MoonProject) -> dict[str, Any]:
    config = _load_project_config(project)
    footage_dir = project.root / str(config.get("footage_dir", "footage"))
    if not footage_dir.exists():
        raise FileNotFoundError(footage_dir)

    items = []
    for path in sorted(footage_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            probe = probe_media(path)
            probe["relative_path"] = str(path.relative_to(project.root))
            items.append(probe)

    return {
        "role": "footage_collection",
        "directory": str(footage_dir),
        "count": len(items),
        "items": items,
    }


def resolve_project_source(project: MoonProject, source: str | Path) -> Path:
    value = Path(source)
    candidate = value if value.is_absolute() else project.root / value
    resolved = candidate.expanduser().resolve()
    try:
        resolved.relative_to(project.root)
    except ValueError as exc:
        raise ValueError("source must be inside the Moon project root") from exc
    if not resolved.exists():
        raise FileNotFoundError(resolved)
    return resolved


def _load_project_config(project: MoonProject) -> dict[str, Any]:
    return json.loads(project.project_path.read_text(encoding="utf-8"))

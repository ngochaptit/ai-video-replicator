from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from moon.atomic import atomic_write_json
from moon.core.project import MoonProject


MIGRATION_VERSION = "project-mirror-v2/1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def migrate_project_mirror_v2(project: MoonProject) -> dict[str, Any]:
    """Inventory legacy state without moving or rewriting any V1 artifact."""
    marker = project.moon_dir / "migrations" / "project-mirror-v2.json"
    if marker.exists():
        return json.loads(marker.read_text(encoding="utf-8"))
    preserved: list[dict[str, Any]] = []
    candidates = [
        project.root / "AGENT" / "request.json",
        project.root / "AGENT" / "response.json",
        project.moon_dir / "bridge-state.json",
        project.moon_dir / "footage-semantic-progress.json",
    ]
    for path in candidates:
        if path.is_file():
            preserved.append(
                {
                    "relative_path": path.relative_to(project.root).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
    progress_path = project.moon_dir / "footage-semantic-progress.json"
    progress = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.exists() else {}
    batches = list(progress.get("batches") or [])
    result = {
        "migration": MIGRATION_VERSION,
        "created_at": _now(),
        "mode": "non_destructive",
        "legacy_files_preserved": preserved,
        "footage_progress": {
            "completed": [item.get("batch_id") for item in batches if item.get("status") == "completed"],
            "pending": [item.get("batch_id") for item in batches if item.get("status") == "pending"],
            "active": next((item.get("batch_id") for item in batches if item.get("status") in {"active", "waiting_gpt"}), None),
        },
        "next_action": "Enable exchange_protocol=project_mirror_v2 after verifying the mirror sync root.",
    }
    atomic_write_json(marker, result)
    return result

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
    """Inventory reusable V1 state without moving or rewriting any source file."""
    marker = project.moon_dir / "migrations" / "project-mirror-v2.json"
    if marker.exists():
        return json.loads(marker.read_text(encoding="utf-8"))
    preserved: list[dict[str, Any]] = []
    candidates = [
        project.state_path,
        project.agent_state_path,
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
    pipeline_state = (
        json.loads(project.state_path.read_text(encoding="utf-8"))
        if project.state_path.exists()
        else {}
    )
    bridge_path = project.moon_dir / "bridge-state.json"
    bridge_state = (
        json.loads(bridge_path.read_text(encoding="utf-8"))
        if bridge_path.exists()
        else {}
    )
    reusable: list[dict[str, Any]] = []
    for root_name, root in (
        ("artifacts", project.artifacts_dir),
        ("evidence", project.evidence_dir),
        ("checkpoints", project.checkpoints_dir),
    ):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                reusable.append(
                    {
                        "kind": root_name,
                        "relative_path": path.relative_to(project.root).as_posix(),
                        "size_bytes": path.stat().st_size,
                        "sha256": _sha256(path),
                    }
                )
    active_request = bridge_state.get("active_request") or {}
    result = {
        "migration": MIGRATION_VERSION,
        "created_at": _now(),
        "mode": "non_destructive",
        "legacy_files_preserved": preserved,
        "pipeline": {
            "status": pipeline_state.get("status"),
            "current_stage": pipeline_state.get("current_stage"),
            "revision": pipeline_state.get("revision"),
            "completed_stages": pipeline_state.get("completed") or [],
        },
        "reusable_files": reusable,
        "legacy_exchange": {
            "active_request": active_request,
            "consumed_request_ids": sorted((bridge_state.get("consumed") or {}).keys()),
        },
        "footage_progress": {
            "completed": [item.get("batch_id") for item in batches if item.get("status") == "completed"],
            "pending": [item.get("batch_id") for item in batches if item.get("status") == "pending"],
            "active": next((item.get("batch_id") for item in batches if item.get("status") in {"active", "waiting_gpt"}), None),
        },
        "next_action": "Enable exchange_protocol=project_mirror_v2 after verifying the mirror sync root.",
    }
    atomic_write_json(marker, result)
    return result

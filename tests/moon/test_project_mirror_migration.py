from __future__ import annotations

import hashlib
import json
from pathlib import Path

from moon.core.project import MoonProject
from moon.project_mirror_migration import migrate_project_mirror_v2


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_migration_is_non_destructive_and_idempotent(tmp_path: Path) -> None:
    project = MoonProject.open(tmp_path / "project", create=True)
    agent = project.root / "AGENT"
    agent.mkdir()
    request = agent / "request.json"
    request.write_text('{"request_id":"legacy"}\n', encoding="utf-8")
    before = digest(request)

    first = migrate_project_mirror_v2(project)
    second = migrate_project_mirror_v2(project)

    assert first == second
    assert first["mode"] == "non_destructive"
    assert digest(request) == before


def test_migration_preserves_completed_and_active_batch_state(tmp_path: Path) -> None:
    project = MoonProject.open(tmp_path / "project", create=True)
    progress = {
        "batches": [
            {"batch_id": "coarse_001", "status": "completed"},
            {"batch_id": "coarse_002", "status": "pending"},
            {"batch_id": "refinement_008", "status": "waiting_gpt"},
        ]
    }
    path = project.moon_dir / "footage-semantic-progress.json"
    path.write_text(json.dumps(progress), encoding="utf-8")
    result = migrate_project_mirror_v2(project)

    assert result["footage_progress"] == {
        "completed": ["coarse_001"],
        "pending": ["coarse_002"],
        "active": "refinement_008",
    }


def test_migration_inventories_reusable_artifacts(tmp_path: Path) -> None:
    project = MoonProject.open(tmp_path / "project", create=True)
    artifact = project.artifacts_dir / "reference_blueprint.json"
    artifact.write_text('{"segments": []}', encoding="utf-8")
    project.state_path.write_text(
        json.dumps({"status": "waiting", "current_stage": "footage", "revision": 2, "completed": ["proposal", "analyze"]}),
        encoding="utf-8",
    )

    result = migrate_project_mirror_v2(project)

    assert result["pipeline"]["completed_stages"] == ["proposal", "analyze"]
    assert result["reusable_files"][0]["relative_path"] == ".moon/artifacts/reference_blueprint.json"

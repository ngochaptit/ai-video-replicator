from __future__ import annotations

import json

import pytest

from moon.bridge import complete_from_imported_artifacts, discover_existing_artifacts, import_existing_artifacts
from moon.core.project import MoonProject
from moon.runner.pipeline import PipelineRunner


def _write_json(path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_discover_existing_analysis_artifacts(tmp_path) -> None:
    _write_json(tmp_path / "analysis" / "reference_blueprint.json", {"segments": []})
    _write_json(tmp_path / "analysis" / "semantic_enrichment.json", {"style": "pov"})

    found = discover_existing_artifacts(tmp_path)

    assert found["reference_blueprint"].endswith("analysis/reference_blueprint.json") or found[
        "reference_blueprint"
    ].endswith("analysis\\reference_blueprint.json")
    assert "semantic_enrichment" in found


def test_import_current_stage_artifacts_into_moon_store(tmp_path) -> None:
    project = MoonProject.open(tmp_path, create=True)
    runner = PipelineRunner(project)
    runner.begin("proposal")
    _write_json(tmp_path / "proposal_packet.json", {"approved": True})

    result = import_existing_artifacts(runner)

    assert result.stage == "proposal"
    assert runner.artifacts.read("proposal_packet")["approved"] is True


def test_complete_from_imported_artifacts_advances_pipeline(tmp_path) -> None:
    project = MoonProject.open(tmp_path, create=True)
    runner = PipelineRunner(project)
    _write_json(tmp_path / "proposal_packet.json", {"approved": True})

    result = complete_from_imported_artifacts(runner)

    assert result["status"]["next_stage"] == "analyze"
    checkpoint = runner.checkpoints.read("proposal")
    assert checkpoint["source"] == "existing_project_artifacts"
    assert "proposal_packet" in checkpoint["artifacts"]


def test_complete_from_artifacts_requires_all_canonical_stage_artifacts(tmp_path) -> None:
    project = MoonProject.open(tmp_path, create=True)
    runner = PipelineRunner(project)
    _write_json(tmp_path / "proposal_packet.json", {"approved": True})
    complete_from_imported_artifacts(runner)
    _write_json(tmp_path / "analysis" / "reference_blueprint.json", {"segments": []})

    with pytest.raises(FileNotFoundError, match="semantic_enrichment"):
        complete_from_imported_artifacts(runner)


def test_bridge_does_not_skip_pipeline_stage(tmp_path) -> None:
    project = MoonProject.open(tmp_path, create=True)
    runner = PipelineRunner(project)
    _write_json(tmp_path / "analysis" / "reference_blueprint.json", {"segments": []})
    _write_json(tmp_path / "analysis" / "semantic_enrichment.json", {"style": "pov"})

    with pytest.raises(ValueError, match="expected 'proposal'"):
        runner.complete("analyze", {"artifacts": {}})

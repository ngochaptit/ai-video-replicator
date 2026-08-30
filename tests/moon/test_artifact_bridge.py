from __future__ import annotations

import json

import pytest

from moon.bridge import (
    bootstrap_legacy_state,
    complete_from_imported_artifacts,
    discover_existing_artifacts,
    import_existing_artifacts,
)
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


def test_bootstrap_legacy_infers_proposal_only_from_complete_analyze_evidence(tmp_path) -> None:
    runner = PipelineRunner(MoonProject.open(tmp_path, create=True))
    _write_json(tmp_path / "analysis" / "reference_blueprint.json", {"segments": []})
    _write_json(tmp_path / "analysis" / "semantic_enrichment.json", {"style": "pov"})

    result = bootstrap_legacy_state(runner)

    assert result.completed == ("proposal", "analyze")
    assert result.inferred == ("proposal",)
    assert result.status["next_stage"] == "footage"
    assert runner.checkpoints.read("proposal")["source"] == "legacy_downstream_evidence"
    assert runner.checkpoints.read("proposal")["evidence_stage"] == "analyze"
    assert runner.artifacts.read("reference_blueprint")["segments"] == []
    assert runner.artifacts.read("semantic_enrichment")["style"] == "pov"


def test_bootstrap_legacy_does_not_infer_from_partial_analyze_evidence(tmp_path) -> None:
    runner = PipelineRunner(MoonProject.open(tmp_path, create=True))
    _write_json(tmp_path / "analysis" / "reference_blueprint.json", {"segments": []})

    result = bootstrap_legacy_state(runner)

    assert result.completed == ()
    assert result.inferred == ()
    assert result.status["next_stage"] == "proposal"


def test_bootstrap_legacy_continues_only_through_contiguous_proven_stages(tmp_path) -> None:
    runner = PipelineRunner(MoonProject.open(tmp_path, create=True))
    _write_json(tmp_path / "proposal_packet.json", {"approved": True})
    _write_json(tmp_path / "analysis" / "reference_blueprint.json", {"segments": []})
    _write_json(tmp_path / "analysis" / "semantic_enrichment.json", {"style": "pov"})
    _write_json(tmp_path / "match_decisions.json", {"matches": []})

    result = bootstrap_legacy_state(runner)

    assert result.completed == ("proposal", "analyze")
    assert result.inferred == ()
    assert result.status["next_stage"] == "footage"
    assert not runner.checkpoints.exists("match")


def test_bootstrap_legacy_refuses_to_rewrite_non_pristine_state(tmp_path) -> None:
    runner = PipelineRunner(MoonProject.open(tmp_path, create=True))
    _write_json(tmp_path / "proposal_packet.json", {"approved": True})
    complete_from_imported_artifacts(runner)

    with pytest.raises(ValueError, match="pristine Moon state"):
        bootstrap_legacy_state(runner)

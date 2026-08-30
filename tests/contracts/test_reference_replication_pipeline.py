from __future__ import annotations

from pathlib import Path

from lib.pipeline_loader import load_pipeline


ROOT = Path(__file__).resolve().parents[2]


def test_reference_replication_pipeline_loads_through_phase_3_without_rendering() -> None:
    manifest = load_pipeline("reference-replication")

    assert manifest["reference_input"]["supported"] is True
    assert [stage["name"] for stage in manifest["stages"]] == ["analyze", "footage", "match", "timeline"]

    analyze, footage, match, timeline = manifest["stages"]
    assert analyze["produces"] == ["reference_blueprint"]
    assert "reference_blueprint_builder" in analyze["required_tools"]

    assert footage["produces"] == ["footage_profiles"]
    assert "footage_profile_builder" in footage["required_tools"]

    assert match["produces"] == ["reference_matching"]
    assert "reference_candidate_ranker" in match["required_tools"]
    assert "reference_match_validator" in match["required_tools"]

    assert timeline["required_artifacts_in"] == ["reference_blueprint", "reference_matching"]
    assert timeline["produces"] == ["replication_timeline"]
    assert timeline["required_tools"] == ["reference_timeline_builder"]

    all_tools = [tool for stage in manifest["stages"] for tool in stage.get("tools_available", [])]
    assert "video_compose" not in all_tools
    assert "clip_search" not in all_tools
    assert manifest["metadata"]["phase"] == 3
    assert manifest["metadata"]["output_contract"] == "schemas/artifacts/replication_timeline.schema.json"


def test_reference_replication_director_skills_exist() -> None:
    skill_dir = ROOT / "skills" / "pipelines" / "reference-replication"

    analyze_text = (skill_dir / "analyze-director.md").read_text(encoding="utf-8")
    assert "choreography beats hard-cut detection" in analyze_text.lower()
    assert "semantic_enrichment_required" in analyze_text

    footage_text = (skill_dir / "footage-director.md").read_text(encoding="utf-8")
    assert "fixed analysis windows are for sampling only" in footage_text.lower()
    assert "does **not** choose final matches" in footage_text

    match_text = (skill_dir / "match-director.md").read_text(encoding="utf-8")
    assert "coverage > perfect matching" in match_text.lower()
    assert "there is no `no_match` final state" in match_text.lower()
    assert "does not render video" in match_text.lower()

    timeline_text = (skill_dir / "timeline-director.md").read_text(encoding="utf-8")
    assert "coverage > perfect matching" in timeline_text.lower()
    assert "do not render in this stage" in timeline_text.lower()
    assert "render_runtime_locked" in timeline_text


def test_phase_2_and_phase_3_artifact_contracts_exist() -> None:
    assert (ROOT / "schemas" / "artifacts" / "footage_profiles.schema.json").is_file()
    assert (ROOT / "schemas" / "artifacts" / "reference_matching.schema.json").is_file()
    assert (ROOT / "schemas" / "artifacts" / "replication_timeline.schema.json").is_file()

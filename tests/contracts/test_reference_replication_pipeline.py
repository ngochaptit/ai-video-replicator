from __future__ import annotations

from pathlib import Path

from lib.pipeline_loader import load_pipeline


ROOT = Path(__file__).resolve().parents[2]


def test_reference_replication_pipeline_loads_and_is_analysis_only() -> None:
    manifest = load_pipeline("reference-replication")

    assert manifest["reference_input"]["supported"] is True
    assert [stage["name"] for stage in manifest["stages"]] == ["analyze"]
    assert manifest["stages"][0]["produces"] == ["reference_blueprint"]
    assert "reference_blueprint_builder" in manifest["stages"][0]["required_tools"]
    assert "video_compose" not in manifest["stages"][0]["tools_available"]
    assert "clip_search" not in manifest["stages"][0]["tools_available"]


def test_reference_replication_director_skill_exists() -> None:
    skill_path = ROOT / "skills" / "pipelines" / "reference-replication" / "analyze-director.md"
    assert skill_path.is_file()
    text = skill_path.read_text(encoding="utf-8")
    assert "choreography beats hard-cut detection" in text.lower()
    assert "semantic_enrichment_required" in text
    assert "does **not** match user footage" in text

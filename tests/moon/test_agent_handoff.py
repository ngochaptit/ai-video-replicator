import json
from pathlib import Path

import pytest

from moon.core.project import MoonProject
from moon.handoff import AgentHandoffService
from moon.runner.pipeline import PipelineRunner


def runner_at(tmp_path: Path) -> PipelineRunner:
    runner = PipelineRunner(MoonProject.open(tmp_path, create=True))
    runner.complete("proposal", {"test": True})
    runner.complete("analyze", {"test": True})
    return runner


def test_footage_handoff_packages_task_and_evidence(tmp_path: Path):
    runner = runner_at(tmp_path)
    evidence = tmp_path / "analysis" / "footage"
    evidence.mkdir(parents=True)
    (evidence / "shot.json").write_text("{}", encoding="utf-8")
    runner.artifacts.write("footage_profiles_scaffold", {"clips": []})
    runner.artifacts.write("footage_agent_task", {"stage": "footage", "evidence_root": str(evidence)})
    package = AgentHandoffService(runner).package()
    assert package["stage"] == "footage"
    assert package["output_contract"]["artifact"] == "footage_semantic_enrichment"
    assert package["inputs"]["footage_profiles_scaffold"]["sha256"]
    assert str(evidence / "shot.json") in package["inputs"]["evidence"]["files"]
    assert package["handoff_id"]


def test_submit_footage_validates_before_persisting(tmp_path: Path):
    runner = runner_at(tmp_path)
    service = AgentHandoffService(runner)
    with pytest.raises(ValueError):
        service.submit("footage", {"clips": []})
    payload = {"clips": [{"path": "footage/one.mp4", "segments": [{"source_in": 1.0, "source_out": 2.0, "semantic": {"action": "pour"}}]}]}
    result = service.submit("footage", payload)
    assert result["accepted"] is True
    assert runner.artifacts.read("footage_semantic_enrichment") == payload


def test_handoff_rejects_stale_stage(tmp_path: Path):
    runner = runner_at(tmp_path)
    runner.artifacts.write("footage_agent_task", {"stage": "footage"})
    with pytest.raises(ValueError):
        AgentHandoffService(runner).submit("match", {"matches": []})


def test_match_contract_keeps_fallback_explicit(tmp_path: Path):
    runner = runner_at(tmp_path)
    runner.complete("footage", {"test": True})
    service = AgentHandoffService(runner)
    with pytest.raises(ValueError):
        service.submit("match", {"matches": [{"reference_segment_id": "r1", "footage_segment_id": "f1", "match_class": "maybe", "scores": {}, "rationale": "x"}]})
    payload = {"matches": [{"reference_segment_id": "r1", "footage_segment_id": "f1", "match_class": "fallback", "scores": {"overall": 0.2}, "rationale": "closest available"}]}
    assert service.submit("match", payload)["artifact"] == "match_proposal"


def test_qc_bundle_is_single_portable_submission(tmp_path: Path):
    runner = runner_at(tmp_path)
    runner.complete("footage", {"test": True}); runner.complete("match", {"test": True}); runner.complete("timeline", {"test": True}); runner.complete("render", {"test": True})
    payload = {"qc_report": {"verdict": "revise"}, "decision_log": {"items": []}}
    result = AgentHandoffService(runner).submit("qc", payload)
    assert result["artifact"] == "qc_bundle"
    assert runner.artifacts.read("qc_bundle") == payload


def test_qc_rejects_pass_that_contradicts_deterministic_quality_failure(tmp_path: Path):
    runner = runner_at(tmp_path)
    runner.complete("footage", {}); runner.complete("match", {}); runner.complete("timeline", {}); runner.complete("render", {})
    runner.artifacts.write("replication_quality_report", _quality_report(gate="fail", source_limited=False))

    with pytest.raises(ValueError, match="contradicts deterministic replication quality failure"):
        AgentHandoffService(runner).submit("qc", {"qc_report": {"decision": "pass"}, "decision_log": {}})


def test_source_limited_quality_uses_footage_limited_without_render_revision(tmp_path: Path):
    runner = runner_at(tmp_path)
    runner.complete("footage", {}); runner.complete("match", {}); runner.complete("timeline", {}); runner.complete("render", {})
    runner.artifacts.write("replication_quality_report", _quality_report(gate="fail", source_limited=True))
    service = AgentHandoffService(runner)

    with pytest.raises(ValueError, match="cannot be fixed by a Moon engine revision"):
        service.submit("qc", {"qc_report": {"decision": "revise"}, "decision_log": {}})

    accepted = service.submit("qc", {"qc_report": {"decision": "footage_limited"}, "decision_log": {}})
    assert accepted["accepted"] is True
    assert runner.state.revision == 0


def _quality_report(*, gate: str, source_limited: bool) -> dict:
    return {
        "revision": 0,
        "quality_gate": gate,
        "render_integrity": {"status": "pass"},
        "replication_quality": {"source_limited": source_limited},
    }

from pathlib import Path

import pytest

from moon.agent_bridge import AgentBridgeService, parse_bridge_request
from moon.core.project import MoonProject
from moon.execution import StageExecutionService
from moon.runner.pipeline import PipelineRunner


def runner_at(tmp_path: Path) -> PipelineRunner:
    runner = PipelineRunner(MoonProject.open(tmp_path, create=True))
    runner.complete("proposal", {"test": True})
    runner.complete("analyze", {"test": True})
    return runner


def test_next_stops_at_agent_boundary_and_packages_handoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runner = runner_at(tmp_path)
    evidence = tmp_path / "analysis" / "footage"
    evidence.mkdir(parents=True)
    (evidence / "brief.json").write_text("{}", encoding="utf-8")
    runner.artifacts.write("footage_profiles_scaffold", {"clips": []})
    runner.artifacts.write(
        "footage_agent_task",
        {
            "stage": "footage",
            "decision_owner": "external_agent",
            "required_output_artifact": "footage_semantic_enrichment",
            "evidence_root": str(evidence),
        },
    )

    def fake_run(self):
        return {"status": "awaiting_agent", "stage": "footage", "pipeline": self.runner.status()}

    monkeypatch.setattr(StageExecutionService, "run", fake_run)
    result = AgentBridgeService(runner).next()
    assert result["status"] == "awaiting_agent"
    assert result["stage"] == "footage"
    assert result["handoff"]["output_contract"]["artifact"] == "footage_semantic_enrichment"
    assert result["history"] == [{"stage": "footage", "status": "awaiting_agent"}]


def test_next_continues_across_completed_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runner = runner_at(tmp_path)
    runner.artifacts.write("match_agent_task", {"stage": "match"})
    runner.artifacts.write("reference_blueprint", {})
    runner.artifacts.write("footage_profiles", {})
    runner.artifacts.write("candidate_rankings", {})
    calls = []

    def fake_run(self):
        stage = self.runner.state.next_stage()
        calls.append(stage)
        if stage == "footage":
            self.runner.complete("footage", {"test": True})
            return {"status": "completed", "stage": "footage"}
        return {"status": "awaiting_agent", "stage": "match"}

    monkeypatch.setattr(StageExecutionService, "run", fake_run)
    result = AgentBridgeService(runner).next()
    assert calls == ["footage", "match"]
    assert result["stage"] == "match"
    assert result["history"] == [
        {"stage": "footage", "status": "completed"},
        {"stage": "match", "status": "awaiting_agent"},
    ]


def test_bridge_submit_accepts_payload_without_temp_file(tmp_path: Path):
    runner = runner_at(tmp_path)
    payload = {
        "clips": [
            {
                "path": "footage/oneshot.mp4",
                "segments": [{"source_in": 1.0, "source_out": 2.0}],
            }
        ]
    }
    result = AgentBridgeService(runner).request(
        {"action": "submit", "stage": "footage", "payload": payload, "auto_next": False}
    )
    assert result["status"] == "accepted"
    assert runner.artifacts.read("footage_semantic_enrichment") == payload


def test_parse_bridge_request_requires_object():
    assert parse_bridge_request('{"action":"next"}') == {"action": "next"}
    with pytest.raises(ValueError):
        parse_bridge_request("")
    with pytest.raises(ValueError):
        parse_bridge_request("[]")

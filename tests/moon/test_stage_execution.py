from __future__ import annotations

from moon.core.project import MoonProject
from moon.execution import StageExecutionService
from moon.runner.pipeline import PipelineRunner


def _runner_at_footage(tmp_path) -> PipelineRunner:
    runner = PipelineRunner(MoonProject.open(tmp_path, create=True))
    runner.complete("proposal", {"approved": True})
    runner.complete("analyze", {"artifacts": {}})
    runner.artifacts.write(
        "reference_blueprint",
        {
            "analysis_meta": {"semantic_enrichment_required": False},
            "source": {"duration_seconds": 1.0},
            "segments": [
                {
                    "id": "seg_001",
                    "start_seconds": 0.0,
                    "end_seconds": 1.0,
                    "duration_seconds": 1.0,
                    "semantic": {},
                    "camera": {},
                    "spatial": {},
                    "motion": {},
                    "edit": {},
                    "text": {},
                    "audio": {},
                }
            ],
        },
    )
    return runner


def test_plan_marks_footage_as_hybrid(tmp_path) -> None:
    service = StageExecutionService(_runner_at_footage(tmp_path))

    plan = service.plan()

    assert plan["stage"] == "footage"
    assert plan["mode"] == "hybrid"
    assert plan["required_agent_artifact"] == "footage_semantic_enrichment"


def test_footage_first_pass_emits_agent_task_without_completing(tmp_path, monkeypatch) -> None:
    runner = _runner_at_footage(tmp_path)
    service = StageExecutionService(runner)

    monkeypatch.setattr(
        service,
        "_execute_tool",
        lambda name, inputs: {
            "success": True,
            "data": {"analysis_meta": {"semantic_enrichment_required": True}, "clips": []},
            "artifacts": [],
            "error": None,
        },
    )

    result = service.run()

    assert result["status"] == "awaiting_agent"
    assert runner.state.next_stage() == "footage"
    assert runner.artifacts.exists("footage_profiles_scaffold")
    assert runner.artifacts.exists("footage_agent_task")


def test_footage_enrichment_completes_stage(tmp_path, monkeypatch) -> None:
    runner = _runner_at_footage(tmp_path)
    runner.artifacts.write("footage_semantic_enrichment", {"clips": [{"clip_id": "clip_001"}]})
    service = StageExecutionService(runner)

    monkeypatch.setattr(
        service,
        "_execute_tool",
        lambda name, inputs: {
            "success": True,
            "data": {"analysis_meta": {"semantic_enrichment_required": False}, "clips": [{"clip_id": "clip_001"}]},
            "artifacts": [],
            "error": None,
        },
    )

    result = service.run()

    assert result["status"] == "completed"
    assert runner.state.next_stage() == "match"
    assert runner.artifacts.exists("footage_profiles")


def test_match_preparation_never_makes_editorial_choice(tmp_path, monkeypatch) -> None:
    runner = _runner_at_footage(tmp_path)
    runner.artifacts.write("footage_profiles", {"analysis_meta": {"semantic_enrichment_required": False}, "clips": []})
    runner.complete("footage", {"artifacts": {}})
    service = StageExecutionService(runner)

    monkeypatch.setattr(
        service,
        "_execute_tool",
        lambda name, inputs: {
            "success": True,
            "data": {"candidate_sets": [{"reference_segment_id": "seg_001", "candidates": []}]},
            "artifacts": [],
            "error": None,
        },
    )

    result = service.run()

    assert result["status"] == "awaiting_agent"
    assert result["task"]["required_output_artifact"] == "match_proposal"
    assert runner.state.next_stage() == "match"
    assert not runner.artifacts.exists("match_decisions")


def test_timeline_stage_is_completed_by_deterministic_tool(tmp_path, monkeypatch) -> None:
    runner = _runner_at_footage(tmp_path)
    runner.complete("footage", {"artifacts": {}})
    runner.artifacts.write("footage_profiles", {"clips": []})
    runner.complete("match", {"artifacts": {}})
    runner.artifacts.write("match_decisions", {"coverage": {"full_coverage": True}, "matches": []})
    service = StageExecutionService(runner)

    monkeypatch.setattr(
        service,
        "_execute_tool",
        lambda name, inputs: {
            "success": True,
            "data": {"coverage": {"full_coverage": True, "timeline_contiguous": True}, "segments": []},
            "artifacts": [],
            "error": None,
        },
    )

    result = service.run()

    assert result["status"] == "completed"
    assert runner.state.next_stage() == "render"
    assert runner.checkpoints.read("timeline")["tool"] == "reference_timeline_builder"


def test_qc_waits_for_external_agent_artifacts(tmp_path) -> None:
    runner = _runner_at_footage(tmp_path)
    runner.complete("footage", {})
    runner.complete("match", {})
    runner.complete("timeline", {})
    runner.complete("render", {})
    service = StageExecutionService(runner)

    result = service.run()

    assert result["status"] == "awaiting_agent"
    assert set(result["task"]["required_output_artifacts"]) == {"qc_report", "decision_log"}
    assert runner.state.next_stage() == "qc"


def test_render_builds_canonical_plan_before_invoking_renderer(tmp_path, monkeypatch) -> None:
    runner = _runner_at_footage(tmp_path)
    runner.complete("footage", {})
    runner.complete("match", {})
    runner.complete("timeline", {})
    runner.artifacts.write("timeline", {"coverage": {"full_coverage": True, "timeline_contiguous": True}, "segments": []})
    runner.artifacts.write("render_plan", {"runtime_approved": True, "render_runtime": "ffmpeg"})
    service = StageExecutionService(runner)
    calls = []

    def execute(name, inputs):
        calls.append((name, inputs))
        if name == "reference_render_plan_builder":
            return {
                "success": True,
                "data": {
                    "runtime_approved": True,
                    "render_runtime": "ffmpeg",
                    "edit_decisions": {"cuts": [{"id": "cut_001"}]},
                    "asset_manifest": {"assets": []},
                },
                "artifacts": [],
                "error": None,
            }
        plan = runner.artifacts.read("replication_render_plan")
        assert plan["edit_decisions"]["cuts"] == [{"id": "cut_001"}]
        return {"success": True, "data": {"output": inputs["output_path"]}, "artifacts": [], "error": None}

    monkeypatch.setattr(service, "_execute_tool", execute)

    result = service.run()

    assert result["status"] == "completed"
    assert [name for name, _ in calls] == ["reference_render_plan_builder", "reference_video_renderer"]
    builder_inputs = calls[0][1]
    assert builder_inputs["render_runtime"] == "ffmpeg"
    assert builder_inputs["renderer_family"] == "documentary-montage"
    assert builder_inputs["composition_mode"] == "templated"
    assert runner.artifacts.exists("replication_render_plan")

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
    runner.artifacts.write("replication_quality_report", _quality_report())
    service = StageExecutionService(runner)

    result = service.run()

    assert result["status"] == "awaiting_agent"
    assert set(result["task"]["required_output_artifacts"]) == {"qc_report", "decision_log"}
    assert result["task"]["quality_gate"] == "pass"
    assert runner.state.next_stage() == "qc"


def test_qc_handoff_builds_and_persists_deterministic_quality_report(tmp_path, monkeypatch) -> None:
    runner = _runner_at_footage(tmp_path)
    runner.complete("footage", {}); runner.complete("match", {}); runner.complete("timeline", {}); runner.complete("render", {})
    runner.artifacts.write("timeline", {"segments": [{"id": "timeline_001"}]})
    runner.artifacts.write("draft_render", {"output": str(tmp_path / "draft.mp4")})
    service = StageExecutionService(runner)
    calls = []

    def execute(name, inputs):
        calls.append((name, inputs))
        assert name == "replication_quality_evaluator"
        return {"success": True, "data": _quality_report(), "artifacts": [], "error": None}

    monkeypatch.setattr(service, "_execute_tool", execute)

    result = service.run()

    assert result["status"] == "awaiting_agent"
    assert runner.artifacts.exists("replication_quality_report")
    assert calls[0][1]["revision"] == 0


def test_source_limited_qc_finalizes_without_consuming_revision(tmp_path) -> None:
    runner = _runner_at_footage(tmp_path)
    runner.complete("footage", {}); runner.complete("match", {}); runner.complete("timeline", {}); runner.complete("render", {})
    draft = tmp_path / "output" / "draft_r0.mp4"; draft.parent.mkdir(parents=True); draft.write_bytes(b"draft")
    quality = _quality_report(gate="fail", source_limited=True)
    runner.artifacts.write("replication_quality_report", quality)
    runner.artifacts.write("draft_render", {"output": str(draft), "revision": 0})
    runner.artifacts.write("qc_bundle", {"qc_report": {"decision": "footage_limited"}, "decision_log": {"items": []}})

    result = StageExecutionService(runner).run()

    assert result["status"] == "completed"
    assert result["quality_gate"] == "fail"
    assert runner.state.revision == 0
    assert runner.status()["done"] is True
    assert (tmp_path / "output" / "final.mp4").read_bytes() == b"draft"
    assert runner.artifacts.read("qc_report")["replication_quality"]["quality_gate"] == "fail"


def _quality_report(*, gate: str = "pass", source_limited: bool = False) -> dict:
    return {
        "revision": 0,
        "decision_count": 1,
        "fallback_count": 1 if source_limited else 0,
        "fallback_ratio": 1.0 if source_limited else 0.0,
        "unique_source_segment_count": 1,
        "reuse_ratio": 0.0,
        "max_reuse_count": 1,
        "dominant_source_share": 1.0,
        "overlap_reuse_count": 0,
        "speed": {"min": 1.0, "max": 1.0, "mean": 1.0, "normal_count": 1, "warning_count": 0, "severe_count": 0, "invalid_count": 0, "timeline_consistency_error_count": 0},
        "chronology": {"backward_jump_count": 0, "large_backward_jump_count": 0, "source_direction_changes": 0, "chronology_consistency_score": 1.0},
        "render_integrity": {"status": "pass"},
        "replication_quality": {"status": gate, "source_limited": source_limited, "fixable_by_render_revision": False, "recommended_route": "footage" if source_limited else "none"},
        "quality_flags": [],
        "quality_gate": gate,
    }


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

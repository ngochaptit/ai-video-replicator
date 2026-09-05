from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from moon.cli import main
from moon.core.project import MoonProject
from moon.drive_bridge import (
    BridgeError,
    BridgeResponseError,
    BridgeTransportError,
    DriveBridgeConfig,
    DuplicateResponseError,
    MoonDriveBridge,
    LocalSyncTransport,
)
from moon.runner.pipeline import PipelineRunner
from moon.execution import StageExecutionService
from moon.handoff import AgentHandoffService
from schemas.artifacts import load_schema
import jsonschema


class MemoryTransport:
    def __init__(self) -> None:
        self.response: bytes | None = None
        self.fail_downloads = 0
        self.published: list[str] = []
        self.request: dict = {}

    def publish(self, request_path: Path, evidence: list[tuple[Path, str]]) -> dict:
        self.request = json.loads(request_path.read_text(encoding="utf-8"))
        self.published = ["request.json", *[relative for _, relative in evidence]]
        return {"transport": "memory", "files": len(self.published)}

    def download_response(self) -> bytes | None:
        if self.fail_downloads:
            self.fail_downloads -= 1
            raise BridgeTransportError("temporary disconnect")
        return self.response

    def upload_request(self, request_path: Path) -> None:
        self.request = json.loads(request_path.read_text(encoding="utf-8"))

    def upload_response(self, response_path: Path) -> None:
        self.response = response_path.read_bytes()

    def status(self) -> dict:
        return {"transport": "memory", "response_present": self.response is not None}


def cold_start_payload(stage):
    if stage == "proposal":
        return {
            "version": "1.0",
            "concept_options": [{"id": f"c{i}", "title": "Pour", "hook": "Watch the pour",
                "narrative_structure": "tutorial", "visual_approach": "Close shots",
                "target_duration_seconds": 2, "why_this_works": "Shows the action"} for i in range(3)],
            "selected_concept": {"concept_id": "c0", "rationale": "User selected"},
            "production_plan": {"pipeline": "reference-replication", "stages": [], "render_runtime": "ffmpeg",
                "taste_profile": {"design_read": "Precise", "visual_variance": 3, "motion_intensity": 4, "information_density": 2}},
            "cost_estimate": {"total_estimated_usd": 0, "line_items": [], "budget_verdict": "within_budget"},
            "approval": {"status": "approved"},
        }
    return {"segments": [{"id": f"seg_{i:03}", "semantic": {
        "actor": "barista", "action": "pour", "object": "water", "target": "cup",
        "interaction": "pour into", "description": "Pour water into cup",
    }} for i in (1, 2)]}


@pytest.fixture
def measured_reference(monkeypatch):
    from tools.analysis.video_analyzer import VideoAnalyzer
    from tools.base_tool import ToolResult

    calls = []
    def analyze(self, inputs):
        calls.append(inputs)
        output = Path(inputs["output_dir"])
        frames = output / "keyframes"
        frames.mkdir(parents=True, exist_ok=True)
        keyframes = []
        for timestamp in (0.0, 1.0, 3.0, 4.0):
            frame = frames / f"frame-{timestamp}.jpg"
            frame.write_bytes(b"test-image")
            keyframes.append({"path": str(frame), "timestamp": timestamp})
        # Deliberately present a source video beside the evidence: never export it.
        (frames / "reference.mp4").write_bytes(b"private source")
        brief = {"source": {"local_path": inputs["source"], "duration_seconds": 4.0},
                 "structure_analysis": {"scenes": [{"scene_index": 0, "start_time": 0.0, "end_time": 4.0}]},
                 "keyframes": keyframes}
        (output / "video_analysis_brief.json").write_text(json.dumps(brief), encoding="utf-8")
        return ToolResult(success=True, data=brief)
    monkeypatch.setattr(VideoAnalyzer, "execute", analyze)
    return calls



@pytest.mark.parametrize("stage,artifact,next_stage", [
    ("proposal", "proposal_packet", "analyze"), ("analyze", "semantic_enrichment", "footage"),
])
def test_cold_start_publish_consume_resume(tmp_path, monkeypatch, measured_reference, stage, artifact, next_stage):
    runner = PipelineRunner(MoonProject.open(tmp_path / "project", create=True))
    if stage == "analyze":
        AgentHandoffService(runner).submit("proposal", cold_start_payload("proposal"))
        StageExecutionService(runner).run()
    execution = StageExecutionService(runner)
    assert execution.run()["status"] == "awaiting_agent"
    task_path = runner.artifacts.path_for(f"{stage}_agent_task")
    original = task_path.stat().st_mtime_ns
    assert execution.run()["task"] == runner.artifacts.read(f"{stage}_agent_task")
    assert task_path.stat().st_mtime_ns == original
    transport = MemoryTransport()
    config = DriveBridgeConfig(project_id="cold-start", transport="local_sync", sync_root=tmp_path / "drive")
    bridge = MoonDriveBridge(runner, config, transport=transport)
    request = bridge.publish(stage)["request"]
    contract = request["expected_response_schema"]["properties"]["payload"]
    if stage == "proposal":
        assert {key: contract[key] for key in load_schema(artifact)} == load_schema(artifact)
    assert contract["artifact"] == artifact
    response = json.loads(completed_response(request))
    response["payload"] = cold_start_payload(stage)
    jsonschema.validate(response, request["expected_response_schema"])
    transport.response = json.dumps(response).encode()
    # Only the downstream media boundary is stubbed; resume uses the real agent bridge.
    def footage_boundary(self):
        self.runner.artifacts.write("footage_agent_task", {"stage": "footage"})
        return {"status": "awaiting_agent", "stage": "footage"}
    monkeypatch.setattr(StageExecutionService, "_run_footage", footage_boundary)
    result = bridge.poll_once()
    assert result["status"] == "CONSUMED"
    assert result["resume"]["stage"] == next_stage
    assert runner.state.next_stage() == next_stage
    assert runner.state.completed.count(stage) == 1
    assert runner.artifacts.read(artifact) == cold_start_payload(stage)
    assert len(measured_reference) == 1
    assert measured_reference[0]["analysis_depth"] == "deep"
    assert measured_reference[0]["source"] == str(runner.project.root / "reference.mp4")
    scaffold = runner.artifacts.read("reference_blueprint_scaffold")
    assert scaffold["analysis_meta"]["semantic_enrichment_required"] is True
    assert [(seg["start_seconds"], seg["end_seconds"]) for seg in scaffold["segments"]] == [(0, 2), (2, 4)]
    if stage == "analyze":
        blueprint = runner.artifacts.read("reference_blueprint")
        jsonschema.validate(blueprint, load_schema("reference_blueprint"))
        assert blueprint["analysis_meta"]["semantic_enrichment_required"] is False
        assert blueprint["source"] == scaffold["source"]
        assert all(seg["semantic"]["action"] == "pour" for seg in blueprint["segments"])
        assert [seg["evidence"] for seg in blueprint["segments"]] == [seg["evidence"] for seg in scaffold["segments"]]
        assert {item.get("artifact") for item in request["evidence"]} >= {"reference_blueprint_scaffold", "video_analysis_brief"}
        images = [item for item in request["evidence"] if item.get("role") == "reference_frame"]
        assert {item["timestamp_seconds"] for item in images} == {0, 1, 3, 4}
        assert all(Path(item["source_path"]).is_file() for item in images)
        assert all(not item["path"].endswith(".mp4") for item in request["evidence"])
        assert len(request["evidence"]) <= config.max_evidence_files
        assert sum(item["bytes"] for item in request["evidence"]) <= config.max_evidence_bytes
    assert PipelineRunner(runner.project).state.next_stage() == next_stage
    assert bridge.publish(next_stage)["request"]["stage"] == next_stage


@pytest.mark.parametrize("stage", ["proposal"])
def test_cold_start_rejects_invalid_schema_and_waits_for_ready_output(tmp_path, stage):
    runner = PipelineRunner(MoonProject.open(tmp_path, create=True))
    if stage == "analyze": runner.complete("proposal", {"test": True})
    handoff = AgentHandoffService(runner)
    with pytest.raises(ValueError, match="requires valid"):
        handoff.submit(stage, {"version": "1.0"})
    assert not runner.artifacts.exists(handoff._required_artifact(stage))
    payload = cold_start_payload(stage)
    if stage == "proposal": payload["approval"]["status"] = "pending"
    else: payload["analysis_meta"]["semantic_enrichment_required"] = True
    with pytest.raises(ValueError, match="before consumption"):
        handoff.submit(stage, payload)
    assert not runner.artifacts.exists(handoff._required_artifact(stage))
    # Existing canonical drafts must also remain resumable without advancing.
    runner.artifacts.write(handoff._required_artifact(stage), payload)
    assert StageExecutionService(runner).run()["status"] == "awaiting_agent"
    assert runner.state.next_stage() == stage


@pytest.mark.parametrize("mutation", ["unmeasured", "forged_evidence", "source_override", "gap", "unknown_id"])
def test_analyze_rejects_ungrounded_enrichment(tmp_path, measured_reference, mutation):
    runner = PipelineRunner(MoonProject.open(tmp_path / "project", create=True))
    runner.complete("proposal", {"test": True})
    StageExecutionService(runner).run()
    bridge = MoonDriveBridge(runner, DriveBridgeConfig(
        project_id="grounded", transport="local_sync", sync_root=tmp_path / "drive"), transport=MemoryTransport())
    request = bridge.publish("analyze")["request"]
    response = json.loads(completed_response(request))
    payload = cold_start_payload("analyze")
    if mutation == "unmeasured":
        payload["segments"][0].update(start_seconds=0, end_seconds=1.5, boundary_basis=["scene_cut"])
        payload["segments"][1].update(start_seconds=1.5, end_seconds=4, boundary_basis=["action_change"])
    elif mutation == "forged_evidence":
        payload["evidence_catalog"] = [{"path": "fake.jpg", "timestamp": 1.5}]
    elif mutation == "source_override":
        payload["source"] = {"duration_seconds": 99}
    elif mutation == "gap":
        payload["segments"] = payload["segments"][1:]
    else:
        payload["segments"][0]["id"] = "invented"
    response["payload"] = payload
    bridge.transport.response = json.dumps(response).encode()
    with pytest.raises(ValueError, match="valid measured semantic enrichment"):
        bridge.poll_once()
    assert not runner.artifacts.exists("semantic_enrichment")
    assert not runner.artifacts.exists("reference_blueprint")
    assert runner.state.next_stage() == "analyze"


def test_analyze_refines_only_measured_boundaries(tmp_path, measured_reference):
    runner = PipelineRunner(MoonProject.open(tmp_path, create=True))
    runner.complete("proposal", {"test": True})
    execution = StageExecutionService(runner)
    execution.run()
    payload = cold_start_payload("analyze")
    payload["segments"][0].update(start_seconds=0, end_seconds=1, boundary_basis=["scene_cut"])
    payload["segments"][1].update(start_seconds=1, end_seconds=4, boundary_basis=["action_change"])
    AgentHandoffService(runner).submit("analyze", payload)
    assert execution.run()["status"] == "completed"
    result = runner.artifacts.read("reference_blueprint")
    assert [(s["start_seconds"], s["end_seconds"]) for s in result["segments"]] == [(0, 1), (1, 4)]
    assert all(s["start_seconds"] <= t <= s["end_seconds"] for s in result["segments"] for t in s["evidence"]["frame_timestamps"])
    assert len(measured_reference) == 1


def test_local_sync_new_stage_archives_response_and_republish_preserves_it(tmp_path, measured_reference):
    runner = PipelineRunner(MoonProject.open(tmp_path / "project", create=True))
    execution = StageExecutionService(runner)
    execution.run()
    config = DriveBridgeConfig(project_id="local", transport="local_sync", sync_root=tmp_path / "drive")
    bridge = MoonDriveBridge(runner, config)
    proposal = bridge.publish("proposal")["request"]
    remote = bridge.transport.remote
    response = json.loads(completed_response(proposal))
    response["payload"] = cold_start_payload("proposal")
    (remote / "response.json").write_text(json.dumps(response), encoding="utf-8")
    assert bridge.poll_once()["resume"]["stage"] == "analyze"
    consumed_bytes = (remote / "response.json").read_bytes()
    analyze = bridge.publish("analyze")["request"]
    assert analyze["request_id"] != proposal["request_id"]
    assert not (remote / "response.json").exists()
    assert [p.read_bytes() for p in (remote / "history").glob("response-*.json")] == [consumed_bytes]
    assert bridge.transport.download_response() is None
    assert all(path.suffix != ".mp4" for path in remote.rglob("*"))
    # A late sync of the old response is still rejected by request identity.
    (remote / "response.json").write_bytes(consumed_bytes)
    with pytest.raises(BridgeResponseError):
        bridge.poll_once()
    assert not runner.artifacts.exists("semantic_enrichment")
    response = json.loads(completed_response(analyze))
    response["payload"] = cold_start_payload("analyze")
    valid_bytes = json.dumps(response).encode()
    (remote / "response.json").write_bytes(valid_bytes)
    restarted = MoonDriveBridge(PipelineRunner(runner.project), config)
    again = restarted.publish("analyze")
    assert again["idempotent"] is True
    assert again["request"]["request_id"] == analyze["request_id"]
    assert (remote / "response.json").read_bytes() == valid_bytes
    assert len(list((remote / "history").glob("response-*.json"))) == 1


def test_analyze_publish_refuses_packet_without_images_within_limits(tmp_path, measured_reference):
    runner = PipelineRunner(MoonProject.open(tmp_path / "project", create=True))
    runner.complete("proposal", {"test": True})
    StageExecutionService(runner).run()
    bridge = MoonDriveBridge(runner, DriveBridgeConfig(
        project_id="limits", transport="local_sync", sync_root=tmp_path / "drive", max_evidence_files=1),
        transport=MemoryTransport())
    with pytest.raises(BridgeError, match="evidence is missing or exceeds"):
        bridge.publish("analyze")
    assert not bridge.transport.published


@pytest.mark.parametrize("stage", ["proposal", "analyze"])
def test_local_sync_new_request_id_clears_old_root_response(tmp_path, stage):
    transport = LocalSyncTransport(DriveBridgeConfig(
        project_id="replace", transport="local_sync", sync_root=tmp_path / "drive"))
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"request_id": "first", "stage": "proposal"}), encoding="utf-8")
    transport.publish(request, [])
    remote_response = transport.remote / "response.json"
    remote_response.write_bytes(b"old response")
    request.write_text(json.dumps({"request_id": "second", "stage": stage}), encoding="utf-8")
    transport.publish(request, [])
    assert not remote_response.exists()
    assert next((transport.remote / "history").glob("response-*.json")).read_bytes() == b"old response"


def bridge_at(tmp_path: Path, *, resume=None) -> tuple[MoonDriveBridge, MemoryTransport, PipelineRunner]:
    project = tmp_path / "project"
    runner = PipelineRunner(MoonProject.open(project, create=True))
    runner.complete("proposal", {"test": True})
    runner.complete("analyze", {"test": True})
    evidence = project / "analysis" / "footage"
    evidence.mkdir(parents=True)
    (evidence / "shot.json").write_text('{"shot": 1}', encoding="utf-8")
    (evidence / "frame.jpg").write_bytes(b"jpeg evidence")
    (evidence / "source.mp4").write_bytes(b"must never leave the project")
    runner.artifacts.write("footage_profiles_scaffold", {"clips": []})
    runner.artifacts.write(
        "footage_agent_task",
        {
            "stage": "footage",
            "decision_owner": "external_agent",
            "required_output_artifact": "footage_semantic_enrichment",
            "evidence_root": str(evidence),
            "instruction": "Describe the measured footage.",
        },
    )
    transport = MemoryTransport()
    config = DriveBridgeConfig(
        project_id="job-123",
        transport="local_sync",
        sync_root=tmp_path / "drive",
        poll_interval_seconds=0.001,
    )
    service = MoonDriveBridge(runner, config, transport=transport, resume=resume or (lambda: {"status": "resumed"}), sleeper=lambda _: None)
    return service, transport, runner


def completed_response(request: dict, *, request_id: str | None = None) -> bytes:
    return json.dumps(
        {
            "version": "1.0",
            "job_id": request["job_id"],
            "request_id": request_id or request["request_id"],
            "stage": request["stage"],
            "status": "COMPLETED",
            "created_at": request["created_at"],
            "payload": {
                "clips": [
                    {
                        "path": "footage/one.mp4",
                        "segments": [{"source_in": 0.0, "source_out": 1.0}],
                    }
                ]
            },
        }
    ).encode("utf-8")


def test_publish_writes_compact_request_and_expected_schema(tmp_path: Path):
    service, transport, _ = bridge_at(tmp_path)

    result = service.publish("footage")

    request = result["request"]
    assert request["job_id"] == "job-123"
    assert request["request_id"]
    assert request["stage"] == "footage"
    assert request["status"] == "WAITING_AGENT"
    assert request["created_at"] and request["expires_at"]
    assert request["expected_response_schema"]["properties"]["payload"]["artifact"] == "footage_semantic_enrichment"
    assert "evidence_root" not in request["task"]
    assert (service.agent_dir / "request.json").is_file()
    assert (service.agent_dir / "evidence").is_dir()
    assert transport.request == request


def test_valid_response_is_validated_consumed_and_resumed_once(tmp_path: Path):
    resumes: list[bool] = []
    service, transport, runner = bridge_at(
        tmp_path, resume=lambda: resumes.append(True) or {"status": "awaiting_agent", "stage": "match"}
    )
    request = service.publish("footage")["request"]
    transport.response = completed_response(request)

    result = service.poll_once()

    assert result["status"] == "CONSUMED"
    assert result["submission"]["accepted"] is True
    assert runner.artifacts.exists("footage_semantic_enrichment")
    assert json.loads(service.response_path.read_text(encoding="utf-8"))["status"] == "CONSUMED"
    assert transport.request["status"] == "CONSUMED"
    assert resumes == [True]


def test_malformed_json_is_rejected(tmp_path: Path):
    service, transport, _ = bridge_at(tmp_path)
    service.publish("footage")
    transport.response = b'{"not":'

    with pytest.raises(BridgeResponseError, match="malformed"):
        service.poll_once()


def test_wrong_request_id_is_rejected(tmp_path: Path):
    service, transport, runner = bridge_at(tmp_path)
    request = service.publish("footage")["request"]
    transport.response = completed_response(request, request_id="wrong-request")

    with pytest.raises(BridgeResponseError, match="request_id"):
        service.poll_once()
    assert not runner.artifacts.exists("footage_semantic_enrichment")


def test_stale_response_is_rejected(tmp_path: Path):
    service, transport, _ = bridge_at(tmp_path)
    request = service.publish("footage")["request"]
    response = json.loads(completed_response(request))
    response["created_at"] = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
    transport.response = json.dumps(response).encode("utf-8")

    with pytest.raises(BridgeResponseError, match="stale"):
        service.poll_once()


def test_response_cannot_add_command_directives(tmp_path: Path):
    service, transport, _ = bridge_at(tmp_path)
    request = service.publish("footage")["request"]
    response = json.loads(completed_response(request))
    response["shell_command"] = "do-not-run"
    transport.response = json.dumps(response).encode("utf-8")

    with pytest.raises(BridgeResponseError, match="unsupported fields"):
        service.poll_once()


def test_duplicate_response_is_rejected_without_second_resume(tmp_path: Path):
    resumes: list[bool] = []
    service, transport, _ = bridge_at(
        tmp_path, resume=lambda: resumes.append(True) or {"status": "resumed"}
    )
    request = service.publish("footage")["request"]
    original = completed_response(request)
    transport.response = original
    service.poll_once()
    transport.response = original

    with pytest.raises(DuplicateResponseError, match="already consumed"):
        service.poll_once()
    republish = service.publish("footage")
    assert republish["status"] == "CONSUMED"
    assert republish["idempotent"] is True
    assert resumes == [True]


def test_watch_reconnects_then_resumes(tmp_path: Path):
    service, transport, _ = bridge_at(tmp_path)
    request = service.publish("footage")["request"]
    transport.response = completed_response(request)
    transport.fail_downloads = 1

    result = service.watch(timeout_seconds=1)

    assert result["status"] == "CONSUMED"
    assert result["reconnects"] == 1


def test_restart_finishes_pending_resume_without_resubmitting(tmp_path: Path):
    resumes: list[bool] = []
    def disconnected_resume():
        raise ConnectionError("runtime temporarily unavailable")

    service, transport, runner = bridge_at(tmp_path, resume=disconnected_resume)
    request = service.publish("footage")["request"]
    original = completed_response(request)
    transport.response = original
    first = service.poll_once()
    assert first["status"] == "CONSUMED_RESUME_PENDING"

    restarted = MoonDriveBridge(
        runner,
        service.config,
        transport=transport,
        resume=lambda: resumes.append(True) or {"status": "resumed"},
        sleeper=lambda _: None,
    )
    transport.response = original
    result = restarted.poll_once()

    assert result["status"] == "CONSUMED"
    assert result["submission"] == {"accepted": False, "duplicate": True}
    assert resumes == [True]


def test_publish_never_uploads_source_video(tmp_path: Path):
    service, transport, _ = bridge_at(tmp_path)

    request = service.publish("footage")["request"]

    assert any(path.endswith("shot.json") for path in transport.published)
    assert any(path.endswith("frame.jpg") for path in transport.published)
    assert all(not path.lower().endswith((".mp4", ".mov", ".mkv")) for path in transport.published)
    assert all(not item["path"].lower().endswith(".mp4") for item in request["evidence"])


def test_cli_bridge_publish_uses_project_positional_argument(tmp_path: Path, capsys):
    _, _, runner = bridge_at(tmp_path)
    config_path = runner.project.moon_dir / "bridge.json"
    config_path.write_text(
        json.dumps(
            {
                "project_id": "job-123",
                "transport": "local_sync",
                "drive": {"sync_root": str(tmp_path / "drive")},
            }
        ),
        encoding="utf-8",
    )

    assert main(["bridge", "publish", str(runner.project.root), "footage"]) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["status"] == "WAITING_AGENT"
    assert output["request"]["stage"] == "footage"
    remote = tmp_path / "drive" / "MON_EDIT" / "jobs" / "job-123" / "AGENT"
    assert (remote / "request.json").is_file()
    assert all(path.suffix.lower() != ".mp4" for path in remote.rglob("*"))

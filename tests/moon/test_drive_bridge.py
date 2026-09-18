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
    MAX_RESPONSE_BYTES,
    MoonDriveBridge,
    LocalSyncTransport,
)
from moon.runner.pipeline import PipelineRunner
from moon.execution import StageExecutionService
from moon.evidence import SampledFrameEvidenceStore
from moon.footage_evidence import FootageEvidencePlanner
from moon.handoff import AgentHandoffService
from schemas.artifacts import load_schema
import jsonschema


class MemoryTransport:
    def __init__(self) -> None:
        self.response: bytes | None = None
        self.fail_downloads = 0
        self.published: list[str] = []
        self.request: dict = {}
        self.archived_responses: list[bytes] = []

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

    def archive_response(self, request_id: str, revision: int) -> None:
        if self.response is not None:
            self.archived_responses.append(self.response)
            self.response = None

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
        from PIL import Image

        calls.append(inputs)
        output = Path(inputs["output_dir"])
        frames = output / "keyframes"
        frames.mkdir(parents=True, exist_ok=True)
        keyframes = []
        for timestamp in (0.0, 1.0, 3.0, 4.0):
            frame = frames / f"frame-{timestamp}.jpg"
            Image.new(
                "RGB",
                (640, 360),
                (int(timestamp * 40) % 255, 80, 160),
            ).save(frame, format="JPEG", quality=95)
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
    with pytest.raises(
        (ValueError, BridgeResponseError),
        match="valid measured semantic enrichment|response schema validation failed",
    ):
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
    assert not (remote / "gemini_handoff.pdf").exists()
    assert any(path.suffix.lower() in {".json", ".jpg"} for path in remote.rglob("*"))
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


def test_reusable_bridge_defaults_allow_large_sampled_evidence_sets(tmp_path: Path):
    service, _, _ = bridge_at(tmp_path)
    evidence_root = service.runner.project.root / "analysis" / "footage"
    for index in range(105):
        (evidence_root / f"extra-{index:03d}.jpg").write_bytes(b"sampled-frame")
    (evidence_root / "large-sampled-evidence.jpg").write_bytes(
        b"x" * (26 * 1024 * 1024)
    )

    request = service.publish("footage")["request"]

    assert service.config.max_evidence_files == 500
    assert service.config.max_evidence_bytes == 512 * 1024 * 1024
    assert len(request["evidence"]) > 100
    assert sum(item["bytes"] for item in request["evidence"]) > 25 * 1024 * 1024


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


def test_response_over_five_mib_below_safety_limit_is_accepted(tmp_path: Path):
    service, transport, runner = bridge_at(tmp_path)
    request = service.publish("footage")["request"]
    transport.response = completed_response(request) + b" " * (6 * 1024 * 1024)

    result = service.poll_once()

    assert MAX_RESPONSE_BYTES == 50 * 1024 * 1024
    assert result["status"] == "CONSUMED"
    assert runner.artifacts.exists("footage_semantic_enrichment")


def test_response_schema_error_includes_specific_json_path(tmp_path: Path):
    service, transport, _ = bridge_at(tmp_path)
    request = service.publish("footage")["request"]
    local_request = json.loads(service.request_path.read_text(encoding="utf-8"))
    local_request["expected_response_schema"]["properties"]["payload"] = {
        "type": "object",
        "required": ["clips"],
        "properties": {
            "clips": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["segments"],
                    "properties": {
                        "segments": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["semantic"],
                                "properties": {
                                    "semantic": {
                                        "type": "object",
                                        "required": ["description"],
                                        "properties": {
                                            "description": {"type": "string"}
                                        },
                                    }
                                },
                            },
                        }
                    },
                },
            }
        },
    }
    service.request_path.write_text(json.dumps(local_request), encoding="utf-8")
    response = json.loads(completed_response(request))
    response["payload"]["clips"][0]["segments"][0]["semantic"] = {
        "description": None
    }
    transport.response = json.dumps(response).encode("utf-8")

    with pytest.raises(
        BridgeResponseError,
        match=r"payload\.clips\[0\]\.segments\[0\]\.semantic\.description",
    ):
        service.poll_once()


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
                "exchange_protocol": "legacy",
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
    assert not (remote / "gemini_handoff.pdf").exists()
    assert all(path.suffix.lower() != ".mp4" for path in remote.rglob("*"))


def test_existing_local_sync_config_stays_legacy_without_opt_in(tmp_path: Path):
    project = MoonProject.open(tmp_path / "project", create=True)
    (project.moon_dir / "bridge.json").write_text(
        json.dumps(
            {
                "project_id": "legacy-project",
                "transport": "local_sync",
                "drive": {"sync_root": str(tmp_path / "drive")},
            }
        ),
        encoding="utf-8",
    )

    assert DriveBridgeConfig.load(project.root).exchange_protocol == "legacy"


def test_proposal_publish_creates_persistent_gpt_route_state(tmp_path: Path):
    runner = PipelineRunner(MoonProject.open(tmp_path / "project", create=True))
    assert StageExecutionService(runner).run()["status"] == "awaiting_agent"
    transport = MemoryTransport()
    bridge = MoonDriveBridge(
        runner,
        DriveBridgeConfig(project_id="proposal-state", transport="local_sync", sync_root=tmp_path / "drive"),
        transport=transport,
    )

    request = bridge.publish("proposal")["request"]
    local_state = json.loads(runner.project.agent_state_path.read_text(encoding="utf-8"))

    assert local_state == request["route"]
    assert local_state["status"] == "WAITING_GPT"
    assert local_state["current_actor"] == "gpt"
    assert local_state["next_actor"] == "moon"
    assert local_state["next_action"] == "CONSUME_RESPONSE"
    assert local_state["expected_output"]["artifact"] == "proposal_packet"
    assert local_state["completion_contract"]["terminal_acknowledgement"].startswith(
        "TASK_COMPLETED job_id=proposal-state"
    )


def analyze_bridge_at(tmp_path: Path, measured_reference) -> tuple[MoonDriveBridge, MemoryTransport, PipelineRunner]:
    runner = PipelineRunner(MoonProject.open(tmp_path / "project", create=True))
    AgentHandoffService(runner).submit("proposal", cold_start_payload("proposal"))
    assert StageExecutionService(runner).run()["status"] == "completed"
    assert StageExecutionService(runner).run()["status"] == "awaiting_agent"
    transport = MemoryTransport()
    bridge = MoonDriveBridge(
        runner,
        DriveBridgeConfig(project_id="analyze-state", transport="local_sync", sync_root=tmp_path / "drive"),
        transport=transport,
        resume=lambda: {"status": "resumed"},
    )
    return bridge, transport, runner


def reviewed_analyze_response(request: dict, decision: str, *, revision: int = 0) -> bytes:
    response = json.loads(completed_response(request))
    response["payload"] = cold_start_payload("analyze")
    response["review"] = {"actor": "gpt", "decision": decision, "revision": revision}
    return json.dumps(response).encode("utf-8")


def test_analyze_publish_routes_directly_to_gpt(tmp_path: Path, measured_reference):
    bridge, _, runner = analyze_bridge_at(tmp_path, measured_reference)

    request = bridge.publish("analyze")["request"]
    route = request["route"]

    assert route["status"] == "WAITING_GPT"
    assert route["current_actor"] == "gpt"
    assert route["next_actor"] == "moon"
    assert route["next_action"] == "CONSUME_RESPONSE"
    assert route["required_inputs"] == [item["path"] for item in request["evidence"]]
    assert route["expected_output"]["artifact"] == "semantic_enrichment"
    assert "response.json" in route["expected_output"]["delivery"]
    assert route["completion_contract"]["on_approval"] == {
        "next_actor": "moon", "next_action": "CONSUME_RESPONSE"
    }
    assert route["completion_contract"]["on_revision"]["required_metadata"] == [
        "segment_id", "reason"
    ]
    assert "actor=gpt decision=APPROVED" in route["completion_contract"]["gpt_response"]["acknowledgements"]["APPROVED"]
    assert "next_actor=moon next_action=RECHECK_TARGETS" in route["completion_contract"]["gpt_response"]["acknowledgements"]["REVISION_REQUIRED"]
    assert "gemini" not in json.dumps(route).lower()
    assert "portable_packet" not in route
    assert json.loads(runner.project.agent_state_path.read_text(encoding="utf-8")) == route


def test_gpt_approval_routes_to_moon_and_resumes(tmp_path: Path, measured_reference):
    bridge, transport, runner = analyze_bridge_at(tmp_path, measured_reference)
    request = bridge.publish("analyze")["request"]
    transport.response = reviewed_analyze_response(request, "APPROVED")

    result = bridge.poll_once()
    route = json.loads(runner.project.agent_state_path.read_text(encoding="utf-8"))

    assert result["status"] == "CONSUMED"
    assert runner.artifacts.exists("semantic_enrichment")
    assert route["status"] == "MOON_CONTINUE"
    assert route["current_actor"] == "moon"
    assert [item["status"] for item in route["transition_history"]][-5:] == [
        "MOON_PREPARE", "WAITING_GPT", "GPT_APPROVED", "WAITING_MOON", "MOON_CONTINUE"
    ]


def test_gpt_revision_routes_back_to_gpt_with_targets_and_preserves_request(
    tmp_path: Path, measured_reference
):
    bridge, transport, runner = analyze_bridge_at(tmp_path, measured_reference)
    request = bridge.publish("analyze")["request"]
    response = json.loads(reviewed_analyze_response(request, "REVISION_REQUIRED"))
    response["review"]["revision_targets"] = [
        {"segment_id": "seg_002", "reason": "Action conflicts with frame evidence"}
    ]
    transport.response = json.dumps(response).encode("utf-8")

    result = bridge.poll_once()
    revised_request = json.loads(bridge.request_path.read_text(encoding="utf-8"))
    route = revised_request["route"]

    assert result["status"] == "WAITING_GPT"
    assert revised_request["request_id"] == request["request_id"]
    assert route["revision"] == 1
    assert route["status"] == "WAITING_GPT"
    assert route["current_actor"] == "gpt"
    assert route["revision_targets"] == response["review"]["revision_targets"]
    assert "revision=1 actor=gpt decision=APPROVED" in route["completion_contract"]["terminal_acknowledgement"]
    assert "portable_packet" not in route
    assert revised_request["task"]["revision"] == 1
    assert not runner.artifacts.exists("semantic_enrichment")
    assert transport.response is None

    retry = bridge.publish("analyze")
    assert retry["idempotent"] is True
    assert retry["request"]["request_id"] == request["request_id"]
    assert retry["request"]["route"] == route


def test_stale_review_revision_cannot_advance_route(tmp_path: Path, measured_reference):
    bridge, transport, runner = analyze_bridge_at(tmp_path, measured_reference)
    request = bridge.publish("analyze")["request"]
    revision = json.loads(reviewed_analyze_response(request, "REVISION_REQUIRED"))
    revision["review"]["revision_targets"] = [{"segment_id": "seg_001", "reason": "Wrong actor"}]
    transport.response = json.dumps(revision).encode("utf-8")
    bridge.poll_once()
    before = json.loads(runner.project.agent_state_path.read_text(encoding="utf-8"))
    revised_request = json.loads(bridge.request_path.read_text(encoding="utf-8"))
    transport.response = reviewed_analyze_response(revised_request, "APPROVED", revision=0)

    with pytest.raises(BridgeResponseError, match="revision does not match"):
        bridge.poll_once()

    assert json.loads(runner.project.agent_state_path.read_text(encoding="utf-8")) == before
    assert not runner.artifacts.exists("semantic_enrichment")


def test_missing_agent_state_reconstructs_from_published_request(tmp_path: Path, measured_reference):
    bridge, _, runner = analyze_bridge_at(tmp_path, measured_reference)
    request = bridge.publish("analyze")["request"]
    runner.project.agent_state_path.unlink()

    status = bridge.status()

    assert status["agent_state"] == request["route"]
    assert status["current_actor"] == "gpt"
    assert status["next_actor"] == "moon"
    assert status["next_action"] == "CONSUME_RESPONSE"
    assert json.loads(runner.project.agent_state_path.read_text(encoding="utf-8")) == request["route"]


def test_stale_agent_state_reconstructs_from_published_request(tmp_path: Path, measured_reference):
    bridge, _, runner = analyze_bridge_at(tmp_path, measured_reference)
    request = bridge.publish("analyze")["request"]
    stale = dict(request["route"])
    stale.update(job_id="other-job", request_id="stale-request", revision=99)
    runner.project.agent_state_path.write_text(json.dumps(stale), encoding="utf-8")

    status = bridge.status()

    assert status["agent_state"] == request["route"]
    assert json.loads(runner.project.agent_state_path.read_text(encoding="utf-8")) == request["route"]


def expire_active_request(
    bridge: MoonDriveBridge, request: dict, *, revision: int | None = None
) -> None:
    expired_at = datetime(2000, 1, 1, tzinfo=timezone.utc).isoformat()
    bridge_state = json.loads(bridge.state_path.read_text(encoding="utf-8"))
    bridge_state["active_request"]["expires_at"] = expired_at
    if revision is not None:
        bridge_state["active_request"]["revision"] = revision
    bridge.state_path.write_text(json.dumps(bridge_state), encoding="utf-8")
    request = dict(request)
    request["expires_at"] = expired_at
    if revision is not None:
        request["route"] = dict(request["route"])
        request["route"]["revision"] = revision
    bridge.request_path.write_text(json.dumps(request), encoding="utf-8")


def test_expired_waiting_request_is_replaced_and_route_uses_fresh_identity(tmp_path: Path):
    bridge, transport, runner = bridge_at(tmp_path)
    stale_request = bridge.publish("footage")["request"]
    stale_response = completed_response(stale_request)
    transport.response = stale_response
    expire_active_request(bridge, stale_request, revision=2)

    result = bridge.publish("footage")
    fresh_request = result["request"]
    local_state = json.loads(runner.project.agent_state_path.read_text(encoding="utf-8"))

    assert result["idempotent"] is False
    assert fresh_request["request_id"] != stale_request["request_id"]
    assert fresh_request["stage"] == stale_request["stage"] == "footage"
    assert fresh_request["created_at"] != stale_request["created_at"]
    assert datetime.fromisoformat(fresh_request["expires_at"].replace("Z", "+00:00")) > datetime.now(timezone.utc)
    assert fresh_request["route"]["request_id"] == fresh_request["request_id"]
    assert fresh_request["route"]["revision"] == 2
    assert fresh_request["task"]["revision"] == 2
    assert local_state == fresh_request["route"]
    assert transport.response is None
    assert transport.archived_responses == [stale_response]
    assert all(fresh_request["request_id"] in item["path"] for item in fresh_request["evidence"])
    bridge_state = json.loads(bridge.state_path.read_text(encoding="utf-8"))
    assert bridge_state["expired"][stale_request["request_id"]]["replacement_request_id"] == fresh_request["request_id"]


def test_fresh_waiting_request_remains_idempotent(tmp_path: Path):
    bridge, transport, _ = bridge_at(tmp_path)
    first = bridge.publish("footage")["request"]
    pending_response = completed_response(first)
    transport.response = pending_response

    result = bridge.publish("footage")

    assert result["idempotent"] is True
    assert result["request"]["request_id"] == first["request_id"]
    assert result["request"]["created_at"] == first["created_at"]
    assert result["request"]["expires_at"] == first["expires_at"]
    assert transport.response == pending_response
    assert transport.archived_responses == []


def test_legacy_gemini_pending_request_is_migrated_to_fresh_gpt_route(tmp_path: Path):
    bridge, transport, runner = bridge_at(tmp_path)
    legacy = bridge.publish("footage")["request"]
    legacy["route"].update(
        status="WAITING_GEMINI",
        current_actor="gemini",
        next_actor="gpt",
        portable_packet="gemini_handoff.pdf",
    )
    bridge.request_path.write_text(json.dumps(legacy), encoding="utf-8")
    runner.project.agent_state_path.write_text(
        json.dumps(legacy["route"]), encoding="utf-8"
    )
    stale_response = completed_response(legacy)
    transport.response = stale_response

    result = bridge.publish("footage")
    fresh = result["request"]

    assert result["idempotent"] is False
    assert fresh["request_id"] != legacy["request_id"]
    assert fresh["route"]["status"] == "WAITING_GPT"
    assert fresh["route"]["current_actor"] == "gpt"
    assert fresh["route"]["next_actor"] == "moon"
    assert "portable_packet" not in fresh["route"]
    assert transport.archived_responses == [stale_response]
    assert json.loads(runner.project.agent_state_path.read_text(encoding="utf-8")) == fresh["route"]


def test_stale_response_cannot_be_consumed_after_expired_request_refresh(tmp_path: Path):
    bridge, transport, runner = bridge_at(tmp_path)
    stale_request = bridge.publish("footage")["request"]
    stale_response = completed_response(stale_request)
    transport.response = stale_response
    expire_active_request(bridge, stale_request)
    fresh_request = bridge.publish("footage")["request"]
    state_before = json.loads(runner.project.agent_state_path.read_text(encoding="utf-8"))
    transport.response = stale_response

    with pytest.raises(BridgeResponseError, match="request_id"):
        bridge.poll_once()

    assert not runner.artifacts.exists("footage_semantic_enrichment")
    assert json.loads(runner.project.agent_state_path.read_text(encoding="utf-8")) == state_before
    assert json.loads(bridge.request_path.read_text(encoding="utf-8"))["request_id"] == fresh_request["request_id"]


def test_bridge_status_marks_expired_waiting_request(tmp_path: Path):
    bridge, _, _ = bridge_at(tmp_path)
    request = bridge.publish("footage")["request"]
    expire_active_request(bridge, request)

    status = bridge.status()

    assert status["request_expired"] is True
    assert status["request_lifecycle"] == "expired"


def test_expired_analyze_request_rebuilds_gpt_route_and_evidence(
    tmp_path: Path, measured_reference
):
    bridge, transport, runner = analyze_bridge_at(tmp_path, measured_reference)
    stale_request = bridge.publish("analyze")["request"]
    stale_response = reviewed_analyze_response(stale_request, "APPROVED")
    transport.response = stale_response
    expire_active_request(bridge, stale_request, revision=1)

    result = bridge.publish("analyze")
    fresh_request = result["request"]

    assert result["idempotent"] is False
    assert fresh_request["request_id"] != stale_request["request_id"]
    assert fresh_request["route"]["request_id"] == fresh_request["request_id"]
    assert fresh_request["route"]["revision"] == 1
    assert fresh_request["task"]["revision"] == 1
    assert fresh_request["route"]["status"] == "WAITING_GPT"
    assert fresh_request["route"]["current_actor"] == "gpt"
    assert fresh_request["route"]["required_inputs"] == [
        item["path"] for item in fresh_request["evidence"]
    ]
    assert all(
        fresh_request["request_id"] in item["path"]
        for item in fresh_request["evidence"]
    )
    assert transport.archived_responses == [stale_response]
    assert json.loads(runner.project.agent_state_path.read_text(encoding="utf-8")) == fresh_request["route"]
    assert "portable_packet" not in fresh_request["route"]
    assert "gemini_handoff.pdf" not in transport.published
    assert not (bridge.agent_dir / "gemini_handoff.pdf").exists()


def test_analyze_request_publishes_every_measured_frame_without_packet(
    tmp_path: Path, measured_reference
):
    bridge, transport, _ = analyze_bridge_at(tmp_path, measured_reference)
    request = bridge.publish("analyze")["request"]
    frame_descriptors = [
        item for item in request["evidence"] if item.get("role") == "reference_frame"
    ]

    assert request["route"]["status"] == "WAITING_GPT"
    assert len(frame_descriptors) == 4
    assert "gemini_handoff.pdf" not in transport.published
    assert all(not path.lower().endswith((".mp4", ".pdf")) for path in transport.published)
    for descriptor in frame_descriptors:
        assert descriptor["path"] in transport.published
        assert descriptor["sha256"]
        assert descriptor["timestamp_seconds"] in {0.0, 1.0, 3.0, 4.0}


def test_analyze_republish_is_idempotent_without_packet(
    tmp_path: Path, measured_reference
):
    bridge, transport, _ = analyze_bridge_at(tmp_path, measured_reference)
    request = bridge.publish("analyze")["request"]
    original_paths = list(transport.published)

    result = bridge.publish("analyze")

    assert result["idempotent"] is True
    assert result["request"] == request
    assert transport.published == original_paths
    assert "gemini_handoff.pdf" not in transport.published
    assert not (bridge.agent_dir / "gemini_handoff.pdf").exists()


def test_nonvisual_publish_does_not_create_or_publish_portable_packet(tmp_path: Path):
    runner = PipelineRunner(MoonProject.open(tmp_path / "project", create=True))
    assert StageExecutionService(runner).run()["status"] == "awaiting_agent"
    transport = MemoryTransport()
    bridge = MoonDriveBridge(
        runner,
        DriveBridgeConfig(
            project_id="nonvisual", transport="local_sync", sync_root=tmp_path / "drive"
        ),
        transport=transport,
    )

    request = bridge.publish("proposal")["request"]

    assert "portable_packet" not in request["route"]
    assert not (bridge.agent_dir / "gemini_handoff.pdf").exists()
    assert "gemini_handoff.pdf" not in transport.published


def footage_bridge_at(
    tmp_path: Path,
) -> tuple[MoonDriveBridge, MemoryTransport, PipelineRunner]:
    from PIL import Image

    project = tmp_path / "project"
    runner = PipelineRunner(MoonProject.open(project, create=True))
    runner.complete("proposal", {"test": True})
    runner.complete("analyze", {"test": True})
    source = project / "footage" / "oneshot.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source video must not be transported")
    scaffold = {
        "clips": [
            {
                "clip_id": "clip_001",
                "path": "footage/oneshot.mp4",
                "duration_seconds": 12.0,
                "segments": [],
            }
        ]
    }
    runner.artifacts.write("footage_profiles_scaffold", scaffold)
    frame_root = project / ".moon" / "cache" / "fixture-frames"
    frame_root.mkdir(parents=True, exist_ok=True)
    frames = []
    for index, timestamp in enumerate((0.0, 6.0, 12.0)):
        path = frame_root / f"coarse-{index}.jpg"
        Image.new("RGB", (640, 360), (40 + index * 60, 90, 150)).save(
            path, format="JPEG", quality=95
        )
        frames.append({"timestamp_seconds": timestamp, "path": str(path)})
    store = SampledFrameEvidenceStore(runner.project, runner.state.revision)
    group_id = store.group_id(
        "footage", source, start_seconds=0.0, end_seconds=12.0, count=3, width=320
    )
    store.register(
        "footage",
        {
            "source": str(source),
            "start_seconds": 0.0,
            "end_seconds": 12.0,
            "count": 3,
            "width": 320,
            "frames": frames,
        },
        group_id=group_id,
        clip_id="clip_001",
        sample_kind="coarse",
        handoff_revision=0,
    )
    planner = FootageEvidencePlanner(runner.project, runner.state.revision)
    runner.artifacts.write(
        "footage_evidence_catalog",
        {
            "version": "1.0",
            "entries": planner.evidence_catalog(scaffold),
            "coverage": planner.coverage_summary(scaffold),
            "policy": "adaptive_uniform_seed_v1",
        },
    )
    analysis = project / "analysis" / "footage"
    analysis.mkdir(parents=True, exist_ok=True)
    (analysis / "video_analysis_brief.json").write_text(
        json.dumps({"clip_id": "clip_001", "duration_seconds": 12.0}),
        encoding="utf-8",
    )
    runner.artifacts.write(
        "footage_agent_task",
        {
            "stage": "footage",
            "revision": runner.state.revision,
            "decision_owner": "external_agent",
            "required_output_artifact": "footage_semantic_enrichment",
            "evidence_root": str(analysis),
            "sampling": {
                "policy": "adaptive_uniform_seed_v1",
                "coverage": planner.coverage_summary(scaffold),
            },
            "instruction": "Review every adaptive frame coarse-to-fine.",
        },
    )
    transport = MemoryTransport()
    bridge = MoonDriveBridge(
        runner,
        DriveBridgeConfig(
            project_id="footage-packet",
            transport="local_sync",
            sync_root=tmp_path / "drive",
        ),
        transport=transport,
        resume=lambda: {"status": "resumed"},
    )
    return bridge, transport, runner


def footage_refinement_response(
    request: dict, *, revision: int | None = None
) -> bytes:
    route_revision = request["route"]["revision"] if revision is None else revision
    response = json.loads(completed_response(request))
    response["revision"] = route_revision
    response["payload"] = {
        "artifact": "footage_refinement_request",
        "requests": [
            {
                "clip_id": "clip_001",
                "start_seconds": 4.0,
                "end_seconds": 5.0,
                "reason": "The hand-to-object contact boundary is ambiguous.",
            }
        ],
    }
    response["review"] = {
        "actor": "gpt",
        "decision": "REQUEST_REFINEMENT",
        "revision": route_revision,
    }
    return json.dumps(response).encode("utf-8")


def footage_approval_response(request: dict) -> bytes:
    response = json.loads(completed_response(request))
    response["revision"] = request["route"]["revision"]
    response["payload"] = {
        "artifact": "footage_semantic_batch",
        "batch_id": request["task"]["batch_id"],
        "clips": [
            {
                "clip_id": "clip_001",
                "path": "footage/oneshot.mp4",
                "usable": True,
                "segments": [
                    {
                        "id": "clip_001_seg_001",
                        "source_in": 0.0,
                        "source_out": 12.0,
                        "boundary_basis": ["clip_bounds"],
                        "evidence": {"frame_timestamps": [0.0, 6.0, 12.0]},
                        "semantic": {"action": "measured action"},
                        "camera": {"shot": "measured"},
                        "spatial": {},
                        "motion": {},
                        "quality": {},
                        "confidence": 0.9,
                    }
                ],
            }
        ]
    }
    response["review"] = {
        "actor": "gpt",
        "decision": "APPROVED",
        "revision": request["route"]["revision"],
    }
    return json.dumps(response).encode("utf-8")


def test_footage_request_contains_all_coarse_frames_and_identity_binding(tmp_path: Path):
    bridge, transport, _ = footage_bridge_at(tmp_path)

    request = bridge.publish("footage")["request"]
    frames = [item for item in request["evidence"] if item.get("role") == "sampled_frame"]

    assert request["route"]["status"] == "WAITING_GPT"
    assert request["route"]["current_actor"] == "gpt"
    assert request["route"]["next_actor"] == "moon"
    assert request["route"]["next_action"] == "CONSUME_RESPONSE"
    assert request["route"]["request_id"] == request["request_id"]
    assert request["route"]["revision"] == 0
    assert request["route"]["batch"]["request_id"] == request["request_id"]
    assert request["task"]["batch_type"] == "coarse"
    assert "portable_packet" not in request["route"]
    assert "gemini_handoff.pdf" not in transport.published
    assert all(not path.lower().endswith((".mp4", ".wav", ".pdf")) for path in transport.published)
    assert len(frames) == 3
    assert any(
        item.get("artifact") == "footage_evidence_manifest"
        for item in request["evidence"]
    )
    jsonschema.validate(
        json.loads(footage_refinement_response(request)),
        request["expected_response_schema"],
    )
    for frame in frames:
        assert frame["clip_id"] == "clip_001"
        assert frame["group_id"]
        assert frame["window_start_seconds"] == 0.0
        assert frame["window_end_seconds"] == 12.0
        assert frame["origin"] == "ffmpeg_single_frame_seek_v1"
        assert frame["path"] in transport.published
        assert frame["sha256"]


def test_footage_republish_is_idempotent_without_packet(tmp_path: Path):
    bridge, transport, _ = footage_bridge_at(tmp_path)
    request = bridge.publish("footage")["request"]
    original_paths = list(transport.published)

    result = bridge.publish("footage")

    assert result["idempotent"] is True
    assert result["request"] == request
    assert transport.published == original_paths
    assert "gemini_handoff.pdf" not in transport.published
    assert not (bridge.agent_dir / "gemini_handoff.pdf").exists()


def test_footage_batch_response_with_wrong_batch_id_is_rejected(tmp_path: Path):
    bridge, transport, runner = footage_bridge_at(tmp_path)
    request = bridge.publish("footage")["request"]
    response = json.loads(footage_approval_response(request))
    response["payload"]["batch_id"] = "coarse_wrong_batch"
    transport.response = json.dumps(response).encode("utf-8")

    with pytest.raises(BridgeResponseError, match="batch_id does not match"):
        bridge.poll_once()

    assert not runner.artifacts.exists("footage_semantic_enrichment")


def test_invalid_gpt_footage_refinement_request_exposes_schema_path(tmp_path: Path):
    bridge, transport, runner = footage_bridge_at(tmp_path)
    request = bridge.publish("footage")["request"]
    response = json.loads(footage_refinement_response(request))
    response["payload"]["requests"][0]["unexpected"] = "unsafe"
    transport.response = json.dumps(response).encode("utf-8")

    with pytest.raises(
        BridgeResponseError,
        match=r"response schema validation failed at payload\.requests\[0\]",
    ):
        bridge.poll_once()

    assert runner.state.revision == 0
    assert request == json.loads(bridge.request_path.read_text(encoding="utf-8"))


def test_moon_samples_refinement_and_republishes_fresh_gpt_evidence(
    tmp_path: Path, monkeypatch
):
    from PIL import Image

    bridge, transport, runner = footage_bridge_at(tmp_path)
    request = bridge.publish("footage")["request"]
    calls: list[dict] = []

    def fake_sample(source, output_dir, *, start_seconds, end_seconds, count, width):
        calls.append(
            {
                "source": source,
                "start_seconds": start_seconds,
                "end_seconds": end_seconds,
                "count": count,
                "width": width,
            }
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        for index in range(count):
            timestamp = start_seconds + (end_seconds - start_seconds) * index / (count - 1)
            path = output_dir / f"refined-{index:02d}.jpg"
            Image.new("RGB", (width, 360), (120, 40 + index, 80)).save(path, "JPEG")
            frames.append({"timestamp_seconds": timestamp, "path": str(path)})
        return {
            "source": str(source),
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
            "count": count,
            "width": width,
            "frames": frames,
        }

    monkeypatch.setattr("moon.footage_refinement.sample_frames", fake_sample)
    transport.response = footage_refinement_response(request)

    result = bridge.poll_once()
    revised = bridge.publish("footage")["request"]
    frames = [item for item in revised["evidence"] if item.get("role") == "sampled_frame"]

    assert result["status"] == "CONSUMED"
    assert result["next_action"] == "NEXT_FOOTAGE_BATCH"
    assert len(calls) == 1
    assert calls[0]["start_seconds"] == 4.0
    assert calls[0]["end_seconds"] == 5.0
    assert calls[0]["count"] == 9
    assert calls[0]["width"] == 640
    assert revised["request_id"] != request["request_id"]
    assert revised["route"]["revision"] == revised["task"]["revision"] == 1
    assert revised["created_at"] != request["created_at"]
    assert json.loads(runner.project.agent_state_path.read_text(encoding="utf-8")) == revised["route"]
    assert len(frames) == 9
    assert "revision=1" in revised["route"]["completion_contract"]["terminal_acknowledgement"]
    assert revised["route"]["current_actor"] == "gpt"
    assert "portable_packet" not in revised["route"]
    assert "gemini_handoff.pdf" not in transport.published
    assert all(frame["path"] in transport.published for frame in frames)
    assert transport.response is None
    assert not runner.artifacts.exists("footage_semantic_enrichment")
    assert revised["task"]["batch_type"] == "refinement"

    stale = footage_refinement_response(request, revision=0)
    transport.response = stale
    with pytest.raises(BridgeResponseError, match="request_id"):
        bridge.poll_once()
    assert json.loads(bridge.request_path.read_text(encoding="utf-8")) == revised

    partial = json.loads(footage_approval_response(revised))
    partial["payload"] = {
        "artifact": "footage_semantic_batch",
        "batch_id": revised["task"]["batch_id"],
        "clips": [{"clip_id": "clip_001", "usable": False}],
    }
    transport.response = json.dumps(partial).encode("utf-8")
    assert bridge.poll_once()["status"] == "CONSUMED"

    final_request = bridge.publish("footage")["request"]
    transport.response = footage_approval_response(final_request)
    assert bridge.poll_once()["status"] == "CONSUMED"
    assert runner.artifacts.read("footage_semantic_enrichment")["clips"][0]["clip_id"] == "clip_001"


def test_expired_footage_request_republishes_evidence_with_fresh_identity(tmp_path: Path):
    bridge, transport, _ = footage_bridge_at(tmp_path)
    stale = bridge.publish("footage")["request"]
    transport.response = footage_refinement_response(stale)
    expire_active_request(bridge, stale)

    fresh = bridge.publish("footage")["request"]

    assert fresh["request_id"] != stale["request_id"]
    assert fresh["route"]["request_id"] == fresh["request_id"]
    assert fresh["route"]["revision"] == stale["route"]["revision"]
    assert all(fresh["request_id"] in item["path"] for item in fresh["evidence"])
    assert "portable_packet" not in fresh["route"]
    assert "gemini_handoff.pdf" not in transport.published
    assert not (bridge.agent_dir / "gemini_handoff.pdf").exists()
    assert transport.response is None


def test_gpt_coarse_batches_use_fresh_requests_and_assemble_after_restart(
    tmp_path: Path,
):
    from PIL import Image

    bridge, transport, runner = footage_bridge_at(tmp_path)
    scaffold = runner.artifacts.read("footage_profiles_scaffold")
    store = SampledFrameEvidenceStore(runner.project, runner.state.revision)
    for index in range(2, 5):
        clip_id = f"clip_{index:03d}"
        source = runner.project.root / "footage" / f"clip-{index}.mp4"
        source.write_bytes(f"source-{index}".encode())
        scaffold["clips"].append(
            {
                "clip_id": clip_id,
                "path": str(source),
                "duration_seconds": 12.0,
                "segments": [],
            }
        )
        frames = []
        for frame_index, timestamp in enumerate((0.0, 6.0, 12.0)):
            path = (
                runner.project.cache_dir
                / "fixture-frames"
                / f"{clip_id}-{frame_index}.jpg"
            )
            Image.new(
                "RGB", (320, 180), (index * 30, frame_index * 30, 90)
            ).save(path, "JPEG")
            frames.append({"timestamp_seconds": timestamp, "path": str(path)})
        group_id = store.group_id(
            "footage",
            source,
            start_seconds=0.0,
            end_seconds=12.0,
            count=3,
            width=320,
        )
        store.register(
            "footage",
            {
                "source": str(source),
                "start_seconds": 0.0,
                "end_seconds": 12.0,
                "count": 3,
                "width": 320,
                "frames": frames,
            },
            group_id=group_id,
            clip_id=clip_id,
            sample_kind="coarse",
            handoff_revision=0,
        )
    runner.artifacts.write("footage_profiles_scaffold", scaffold)

    first = bridge.publish("footage")["request"]
    first_clips = set(first["task"]["batch_clip_ids"])
    first_frames = [
        item for item in first["evidence"] if item.get("role") == "sampled_frame"
    ]
    assert len(first_clips) == 3
    assert len(first_frames) <= bridge.config.max_footage_batch_frames
    assert sum(item["bytes"] for item in first_frames) <= bridge.config.max_footage_batch_bytes
    assert first["route"]["status"] == "WAITING_GPT"
    assert first["route"]["batch"]["request_id"] == first["request_id"]
    assert {
        item["clip_id"]
        for item in first["evidence"]
        if item.get("role") == "sampled_frame"
    } == first_clips
    assert all(
        not item["path"].lower().endswith((".mp4", ".wav", ".pdf"))
        for item in first["evidence"]
    )
    first_response = json.loads(footage_approval_response(first))
    first_response["payload"]["clips"] = [
        {
            **first_response["payload"]["clips"][0],
            "clip_id": clip_id,
            "path": next(
                clip["path"] for clip in scaffold["clips"]
                if clip["clip_id"] == clip_id
            ),
            "segments": [
                {
                    **first_response["payload"]["clips"][0]["segments"][0],
                    "id": f"{clip_id}_seg_001",
                }
            ],
        }
        for clip_id in sorted(first_clips)
    ]
    transport.response = json.dumps(first_response).encode()
    assert bridge.poll_once()["remaining_batches"] == 1
    assert not runner.artifacts.exists("footage_semantic_enrichment")

    restarted = MoonDriveBridge(
        runner,
        bridge.config,
        transport=transport,
        resume=lambda: {"status": "resumed"},
    )
    second = restarted.publish("footage")["request"]
    second_clips = set(second["task"]["batch_clip_ids"])
    assert second["request_id"] != first["request_id"]
    assert first_clips.isdisjoint(second_clips)
    assert second["route"]["current_actor"] == "gpt"
    transport.response = json.dumps(first_response).encode()
    with pytest.raises(DuplicateResponseError):
        restarted.poll_once()

    second_response = json.loads(footage_approval_response(second))
    clip_id = next(iter(second_clips))
    second_response["payload"]["clips"][0]["clip_id"] = clip_id
    second_response["payload"]["clips"][0]["path"] = next(
        clip["path"] for clip in scaffold["clips"] if clip["clip_id"] == clip_id
    )
    second_response["payload"]["clips"][0]["segments"][0]["id"] = (
        f"{clip_id}_seg_001"
    )
    transport.response = json.dumps(second_response).encode()
    assert restarted.poll_once()["status"] == "CONSUMED"

    final = runner.artifacts.read("footage_semantic_enrichment")
    assert {clip["clip_id"] for clip in final["clips"]} == {
        "clip_001", "clip_002", "clip_003", "clip_004"
    }

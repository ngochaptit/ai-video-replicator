from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from moon.cli import main
from moon.core.project import MoonProject
from moon.drive_bridge import (
    BridgeResponseError,
    BridgeTransportError,
    DriveBridgeConfig,
    DuplicateResponseError,
    MoonDriveBridge,
)
from moon.runner.pipeline import PipelineRunner


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

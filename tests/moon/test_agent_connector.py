from pathlib import Path
import base64

import pytest

from moon.connector import AgentConnectorService, parse_connector_request
from moon.core.project import MoonProject
from moon.runner.pipeline import PipelineRunner


def runner_at(tmp_path: Path) -> PipelineRunner:
    runner = PipelineRunner(MoonProject.open(tmp_path, create=True))
    runner.complete("proposal", {"test": True})
    runner.complete("analyze", {"test": True})
    return runner


def prepare_footage_boundary(runner: PipelineRunner, tmp_path: Path) -> Path:
    evidence = tmp_path / "analysis" / "footage"
    evidence.mkdir(parents=True)
    (evidence / "brief.json").write_text('{"summary":"measured"}', encoding="utf-8")
    (evidence / "frame_0000.jpg").write_bytes(b"not-a-real-image")
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
    return evidence


def test_manifest_exposes_stable_agent_tools(tmp_path: Path):
    service = AgentConnectorService(runner_at(tmp_path))
    manifest = service.manifest()
    names = {item["name"] for item in manifest["tools"]}
    assert manifest["transport"] == "stdin_stdout_json"
    assert {
        "moon.status", "moon.next", "moon.handoff", "moon.evidence.list",
        "moon.evidence.read_json", "moon.evidence.read_image", "moon.evidence.clear_sampled",
        "moon.frames.sample", "moon.submit",
    } <= names
    assert manifest["semantic_owner"] == "external_agent"


def test_evidence_list_returns_agent_readable_types(tmp_path: Path):
    runner = runner_at(tmp_path)
    prepare_footage_boundary(runner, tmp_path)
    result = AgentConnectorService(runner).call({"tool": "moon.evidence.list", "arguments": {}})
    kinds = {item["kind"] for item in result["files"]}
    assert result["stage"] == "footage"
    assert "json" in kinds
    assert "image" in kinds
    assert all(not Path(item["path"]).is_absolute() for item in result["files"])


def test_read_json_is_restricted_to_project_root(tmp_path: Path):
    runner = runner_at(tmp_path)
    evidence = prepare_footage_boundary(runner, tmp_path)
    service = AgentConnectorService(runner)
    result = service.call({"tool": "moon.evidence.read_json", "arguments": {"path": str(evidence / "brief.json")}})
    assert result["data"]["summary"] == "measured"
    outside = tmp_path.parent / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        service.call({"tool": "moon.evidence.read_json", "arguments": {"path": str(outside)}})


def test_read_image_returns_base64_and_mime_type(tmp_path: Path):
    runner = runner_at(tmp_path)
    evidence = prepare_footage_boundary(runner, tmp_path)
    service = AgentConnectorService(runner)
    result = service.call({"tool": "moon.evidence.read_image", "arguments": {"path": str(evidence / "frame_0000.jpg")}})
    assert result["mime_type"] == "image/jpeg"
    assert result["size_bytes"] == len(b"not-a-real-image")
    assert base64.b64decode(result["data_base64"]) == b"not-a-real-image"
    with pytest.raises(ValueError):
        service.call({"tool": "moon.evidence.read_image", "arguments": {"path": str(evidence / "brief.json")}})


def test_submit_uses_existing_handoff_validation(tmp_path: Path):
    runner = runner_at(tmp_path)
    service = AgentConnectorService(runner)
    payload = {
        "clips": [
            {
                "path": "footage/oneshot.mp4",
                "segments": [{"source_in": 1.0, "source_out": 2.0}],
            }
        ]
    }
    result = service.call({
        "tool": "moon.submit",
        "arguments": {"stage": "footage", "payload": payload, "auto_next": False},
    })
    assert result["status"] == "accepted"
    assert runner.artifacts.read("footage_semantic_enrichment") == payload


def test_connector_request_parser_requires_object():
    assert parse_connector_request('{"tool":"moon.status"}') == {"tool": "moon.status"}
    with pytest.raises(ValueError):
        parse_connector_request("")
    with pytest.raises(ValueError):
        parse_connector_request("[]")

from __future__ import annotations

import json

import pytest

from moon.core.project import MoonProject
from moon.protocol import MoonProtocol
from moon.runner.pipeline import PipelineRunner


def test_project_initializes_persistent_layout(tmp_path) -> None:
    project = MoonProject.open(tmp_path, create=True)
    runner = PipelineRunner(project)

    assert project.project_path.exists()
    assert project.state_path.exists()
    assert project.checkpoints_dir.is_dir()
    assert project.artifacts_dir.is_dir()
    assert project.cache_dir.is_dir()
    assert runner.status()["next_stage"] == "proposal"


def test_runner_resumes_from_first_incomplete_stage(tmp_path) -> None:
    project = MoonProject.open(tmp_path, create=True)
    runner = PipelineRunner(project)

    assert runner.begin() == "proposal"
    runner.complete("proposal", {"approved": True})
    assert runner.begin() == "analyze"
    runner.fail("analyze", "agent quota exhausted")

    fresh_runner = PipelineRunner(MoonProject.open(tmp_path))
    status = fresh_runner.resume()

    assert status["next_stage"] == "analyze"
    assert status["completed"] == ["proposal"]
    assert fresh_runner.checkpoints.read("proposal") == {"approved": True}


def test_protocol_persists_agent_artifacts(tmp_path) -> None:
    runner = PipelineRunner(MoonProject.open(tmp_path, create=True))
    protocol = MoonProtocol(runner)

    response = protocol.handle(
        {
            "action": "artifact.write",
            "name": "match_decision_seg_007",
            "payload": {
                "reference_segment_id": "seg_007",
                "source": "footage/oneshot.mp4",
                "source_in": 128.2,
                "source_out": 132.6,
            },
        }
    )

    assert response["ok"] is True
    stored = protocol.handle({"action": "artifact.read", "name": "match_decision_seg_007"})
    assert stored["result"]["source_in"] == 128.2


def test_runner_rejects_skipping_a_stage(tmp_path) -> None:
    runner = PipelineRunner(MoonProject.open(tmp_path, create=True))

    with pytest.raises(ValueError, match="next resumable stage"):
        runner.begin("match")


def test_state_file_is_human_readable_json(tmp_path) -> None:
    project = MoonProject.open(tmp_path, create=True)
    PipelineRunner(project)

    payload = json.loads(project.state_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["stages"][0] == "proposal"

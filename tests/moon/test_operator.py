from __future__ import annotations

import json
from pathlib import Path

import pytest

from moon.core.project import MoonProject
from moon.core.state import DEFAULT_STAGES, PipelineState
from moon.operator import (
    COMPLETED,
    FAILED,
    PENDING,
    RUNNING,
    WAITING_AGENT,
    DuplicateProjectRun,
    OperatorWorkerProcess,
    OperatorWorker,
    ProjectRunLock,
    WorkerLaunchError,
    inspect_operator_project,
    stage_status_mapping,
    validate_operator_project,
)
from moon.runner.pipeline import PipelineRunner


def valid_project(tmp_path: Path) -> Path:
    root = tmp_path / "operator-project"
    (root / "footage").mkdir(parents=True)
    (root / "reference.mp4").write_bytes(b"reference")
    (root / "footage" / "clip.mov").write_bytes(b"footage")
    return root


def fake_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    python = repository / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    return repository


def test_operator_project_validation(tmp_path: Path):
    root = tmp_path / "bad-project"
    root.mkdir()

    invalid = validate_operator_project(root)

    assert invalid.valid is False
    assert any("reference.mp4" in item for item in invalid.errors)
    assert any("footage" in item for item in invalid.errors)

    (root / "reference.mp4").write_bytes(b"reference")
    (root / "footage").mkdir()
    (root / "footage" / "notes.txt").write_text("not video", encoding="utf-8")
    assert validate_operator_project(root).valid is False
    (root / "footage" / "take.webm").write_bytes(b"video")

    valid = validate_operator_project(root)
    assert valid.valid is True
    assert valid.footage_count == 1


def test_fresh_project_is_ready_and_worker_launch_initializes_moon(tmp_path: Path):
    root = valid_project(tmp_path)
    process_calls = []

    class Process:
        def poll(self):
            return None

    def popen(*args, **kwargs):
        process_calls.append((args, kwargs))
        return Process()

    before = inspect_operator_project(root)
    launcher = OperatorWorkerProcess(fake_repository(tmp_path), popen=popen)
    launcher.start(root)

    assert before["status"] == "ready"
    assert all(item["status"] == PENDING for item in before["stages"])
    assert (root / ".moon" / "state.json").is_file()
    assert process_calls[0][0][0][-3:] == [
        "-m", "moon.operator_worker", str(root.resolve())
    ]


def test_existing_project_is_resumable_from_saved_stage(tmp_path: Path):
    root = valid_project(tmp_path)
    runner = PipelineRunner(MoonProject.open(root, create=True))
    runner.complete("proposal", {"test": True})
    runner.complete("analyze", {"test": True})

    snapshot = inspect_operator_project(root)
    by_stage = {item["stage"]: item["status"] for item in snapshot["stages"]}

    assert snapshot["status"] == "ready"
    assert snapshot["stage"] == "footage"
    assert snapshot["can_start"] is True
    assert by_stage["proposal"] == COMPLETED
    assert by_stage["analyze"] == COMPLETED
    assert by_stage["footage"] == PENDING


def test_stage_transition_status_mapping():
    state = PipelineState(completed=["proposal", "analyze"], current_stage="footage", status="running")

    running = {item["stage"]: item["status"] for item in stage_status_mapping(state)}
    waiting = {
        item["stage"]: item["status"]
        for item in stage_status_mapping(
            state, activity="waiting_agent", activity_stage="footage"
        )
    }
    failed = {
        item["stage"]: item["status"]
        for item in stage_status_mapping(state, activity="failed", activity_stage="footage")
    }

    assert running["proposal"] == running["analyze"] == COMPLETED
    assert running["footage"] == RUNNING
    assert waiting["footage"] == WAITING_AGENT
    assert failed["footage"] == FAILED
    assert running["match"] == PENDING


def test_waiting_agent_state_and_current_gemini_packet_are_detected(tmp_path: Path):
    root = valid_project(tmp_path)
    runner = PipelineRunner(MoonProject.open(root, create=True))
    runner.complete("proposal", {"test": True})
    request_id = "request-current"
    (root / "AGENT").mkdir()
    (root / "AGENT" / "gemini_handoff.pdf").write_bytes(b"%PDF")
    (root / "AGENT" / "request.json").write_text(
        json.dumps(
            {
                "request_id": request_id,
                "route": {"revision": 0},
            }
        ),
        encoding="utf-8",
    )
    (root / ".moon" / "bridge-state.json").write_text(
        json.dumps(
            {
                "active_request": {
                    "request_id": request_id,
                    "stage": "analyze",
                    "status": "WAITING_AGENT",
                    "revision": 0,
                }
            }
        ),
        encoding="utf-8",
    )
    (root / ".moon" / "agent-state.json").write_text(
        json.dumps({"current_actor": "gemini"}), encoding="utf-8"
    )

    snapshot = inspect_operator_project(root)
    by_stage = {item["stage"]: item["status"] for item in snapshot["stages"]}

    assert snapshot["status"] == "waiting_agent"
    assert snapshot["can_start"] is True  # no live worker; reopening can resume it
    assert snapshot["portable_packet"] == str(root / "AGENT" / "gemini_handoff.pdf")
    assert by_stage["analyze"] == WAITING_AGENT
    assert snapshot["debug"]["request_id"] == request_id


def test_complete_state_requires_and_exposes_final_video(tmp_path: Path):
    root = valid_project(tmp_path)
    project = MoonProject.open(root, create=True)
    state = PipelineState(completed=list(DEFAULT_STAGES), status="complete")
    state.save(project.state_path)
    final = root / "output" / "final.mp4"
    final.parent.mkdir()
    final.write_bytes(b"final")

    snapshot = inspect_operator_project(root)

    assert snapshot["status"] == "complete"
    assert snapshot["final_path"] == str(final)
    assert snapshot["can_start"] is False
    assert all(item["status"] == COMPLETED for item in snapshot["stages"])


def test_complete_pipeline_without_final_output_is_failed(tmp_path: Path):
    root = valid_project(tmp_path)
    project = MoonProject.open(root, create=True)
    PipelineState(completed=list(DEFAULT_STAGES), status="complete").save(project.state_path)

    snapshot = inspect_operator_project(root)

    assert snapshot["status"] == "failed"
    assert "final.mp4" in snapshot["message"]
    assert snapshot["can_start"] is False


def test_bridge_worker_subprocess_launch_failure_is_human_readable(tmp_path: Path):
    root = valid_project(tmp_path)

    def failed_popen(*args, **kwargs):
        raise OSError("CreateProcess failed")

    launcher = OperatorWorkerProcess(fake_repository(tmp_path), popen=failed_popen)

    with pytest.raises(WorkerLaunchError, match="Không thể khởi động Moon"):
        launcher.start(root)


def test_bridge_worker_nonzero_exit_is_reported(tmp_path: Path):
    root = valid_project(tmp_path)

    class FailedProcess:
        def poll(self):
            return 7

    launcher = OperatorWorkerProcess(
        fake_repository(tmp_path), popen=lambda *args, **kwargs: FailedProcess()
    )
    launcher.start(root)

    assert launcher.failure() == "Moon worker stopped unexpectedly (exit code 7)."


def test_worker_surfaces_missing_bridge_setup_without_raw_traceback(tmp_path: Path):
    root = valid_project(tmp_path)

    exit_code = OperatorWorker(root, sleep=lambda _: None).run()
    status = json.loads(
        (root / ".moon" / "operator-status.json").read_text(encoding="utf-8")
    )

    assert exit_code == 1
    assert status["status"] == "failed"
    assert "Google Drive" in status["message"]
    assert "bridge.json" not in status["message"]


def test_duplicate_launcher_run_for_same_project_is_prevented(tmp_path: Path):
    root = valid_project(tmp_path)
    MoonProject.open(root, create=True)
    lock = ProjectRunLock(root)
    lock.acquire()
    calls = []
    launcher = OperatorWorkerProcess(
        fake_repository(tmp_path), popen=lambda *args, **kwargs: calls.append(args)
    )
    try:
        with pytest.raises(DuplicateProjectRun, match="đang được"):
            launcher.start(root)
    finally:
        lock.release()

    assert calls == []


def test_stale_run_lock_is_recovered(tmp_path: Path, monkeypatch):
    root = valid_project(tmp_path)
    MoonProject.open(root, create=True)
    lock = ProjectRunLock(root)
    lock.path.write_text(json.dumps({"pid": 99999999, "token": "stale"}), encoding="utf-8")
    monkeypatch.setattr(ProjectRunLock, "_pid_running", staticmethod(lambda pid: False))

    lock.acquire()
    try:
        assert json.loads(lock.path.read_text(encoding="utf-8"))["token"] == lock.token
    finally:
        lock.release()
    assert not lock.path.exists()

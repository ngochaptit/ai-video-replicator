from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import moon.operator as operator_module
from moon.core.project import MoonProject
from moon.core.state import DEFAULT_STAGES, PipelineState
from moon.operator import (
    COMPLETED,
    FAILED,
    LOCAL_PROCESSING,
    PENDING,
    READY,
    RESPONSE_RECEIVED,
    RUNNING,
    WAITING_CHATGPT,
    WAITING_AGENT,
    WAITING_GEMINI,
    WAITING_GPT_AFTER_GEMINI,
    DuplicateProjectRun,
    OperatorStatusStore,
    OperatorWebConfig,
    OperatorWorkerProcess,
    OperatorWorker,
    ProjectRunLock,
    WorkerLaunchError,
    inspect_operator_project,
    stage_status_mapping,
    validate_operator_project,
)
from moon.drive_bridge import BridgeTransportError, DriveBridgeConfig
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


def advance_to(root: Path, stage: str) -> PipelineRunner:
    runner = PipelineRunner(MoonProject.open(root, create=True))
    for item in DEFAULT_STAGES:
        if item == stage:
            break
        runner.complete(item, {"test": True})
    return runner


def write_waiting_route(
    root: Path,
    stage: str,
    *,
    actor: str,
    request_id: str = "request-current",
    revision: int = 0,
    packet_bytes: bytes | None = None,
) -> dict:
    route = {
        "job_id": root.name,
        "request_id": request_id,
        "stage": stage,
        "revision": revision,
        "status": "WAITING_GEMINI" if actor == "gemini" else "WAITING_GPT",
        "current_actor": actor,
        "next_actor": "gpt" if actor == "gemini" else "moon",
        "next_action": "REVIEW_GEMINI_ANALYSIS" if actor == "gemini" else "CONSUME_RESPONSE",
    }
    agent = root / "AGENT"
    agent.mkdir(exist_ok=True)
    if packet_bytes is not None:
        packet = agent / "gemini_handoff.pdf"
        packet.write_bytes(packet_bytes)
        route.update(
            portable_packet="gemini_handoff.pdf",
            portable_packet_manifest={
                "request_id": request_id,
                "stage": stage,
                "revision": revision,
                "sha256": hashlib.sha256(packet_bytes).hexdigest(),
            },
        )
    request = {
        "job_id": root.name,
        "request_id": request_id,
        "stage": stage,
        "status": "WAITING_AGENT",
        "route": route,
    }
    (agent / "request.json").write_text(json.dumps(request), encoding="utf-8")
    (root / ".moon" / "bridge-state.json").write_text(
        json.dumps(
            {
                "active_request": {
                    "job_id": root.name,
                    "request_id": request_id,
                    "stage": stage,
                    "status": "WAITING_AGENT",
                    "revision": revision,
                }
            }
        ),
        encoding="utf-8",
    )
    (root / ".moon" / "agent-state.json").write_text(json.dumps(route), encoding="utf-8")
    return request


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

    stopped = {item["stage"]: item["status"] for item in stage_status_mapping(state)}
    running = {
        item["stage"]: item["status"]
        for item in stage_status_mapping(state, worker_active=True)
    }
    stale_activity = {
        item["stage"]: item["status"]
        for item in stage_status_mapping(
            state, activity="running", activity_stage="footage", worker_active=False
        )
    }
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

    assert stopped["footage"] == READY
    assert stale_activity["footage"] == READY
    assert running["proposal"] == running["analyze"] == COMPLETED
    assert running["footage"] == RUNNING
    assert waiting["footage"] == WAITING_AGENT
    assert failed["footage"] == FAILED
    assert running["match"] == PENDING


def test_waiting_agent_state_and_current_gemini_packet_are_detected(tmp_path: Path):
    root = valid_project(tmp_path)
    advance_to(root, "analyze")
    request_id = "request-current"
    write_waiting_route(
        root, "analyze", actor="gemini", request_id=request_id, packet_bytes=b"%PDF"
    )

    snapshot = inspect_operator_project(root)
    by_stage = {item["stage"]: item["status"] for item in snapshot["stages"]}

    assert snapshot["status"] == "waiting_agent"
    assert snapshot["can_start"] is True  # no live worker; reopening can resume it
    assert snapshot["portable_packet"] == str(root / "AGENT" / "gemini_handoff.pdf")
    assert snapshot["current_task"]["state"] == WAITING_GEMINI
    assert snapshot["current_task"]["owner"] == "GEMINI"
    assert by_stage["analyze"] == WAITING_AGENT
    assert snapshot["debug"]["request_id"] == request_id


def test_pipeline_running_without_worker_is_ready_not_running(tmp_path: Path):
    root = valid_project(tmp_path)
    runner = advance_to(root, "footage")
    runner.state.status = "running"
    runner.state.current_stage = "footage"
    runner.state.save(runner.project.state_path)

    snapshot = inspect_operator_project(root)
    by_stage = {item["stage"]: item["status"] for item in snapshot["stages"]}

    assert snapshot["worker_active"] is False
    assert snapshot["status"] == "ready"
    assert snapshot["current_task"]["state"] == LOCAL_PROCESSING
    assert snapshot["current_task"]["title"] == "SẴN SÀNG TIẾP TỤC"
    assert by_stage["footage"] == READY
    assert RUNNING not in by_stage.values()


@pytest.mark.parametrize("stage", ["analyze", "footage"])
def test_visual_stage_ownership_comes_from_current_route(tmp_path: Path, stage: str):
    root = valid_project(tmp_path)
    advance_to(root, stage)
    write_waiting_route(root, stage, actor="gemini", packet_bytes=b"current-pdf")

    snapshot = inspect_operator_project(root)

    assert snapshot["current_task"]["state"] == WAITING_GEMINI
    assert snapshot["current_task"]["owner"] == "GEMINI"
    assert snapshot["route"]["current_actor"] == "gemini"


def test_visual_route_waiting_for_gpt_is_explicit(tmp_path: Path):
    root = valid_project(tmp_path)
    advance_to(root, "analyze")
    write_waiting_route(root, "analyze", actor="gpt")

    snapshot = inspect_operator_project(root)

    assert snapshot["current_task"]["state"] == WAITING_GPT_AFTER_GEMINI
    assert snapshot["current_task"]["owner"] == "CHATGPT"
    assert snapshot["portable_packet"] is None


def test_gpt_only_match_route_is_explicit(tmp_path: Path):
    root = valid_project(tmp_path)
    advance_to(root, "match")
    write_waiting_route(root, "match", actor="gpt")

    snapshot = inspect_operator_project(root)

    assert snapshot["status"] == "waiting_agent"
    assert snapshot["current_task"]["state"] == WAITING_CHATGPT
    assert snapshot["current_task"]["owner"] == "CHATGPT"
    assert snapshot["portable_packet"] is None


def test_changed_or_stale_packet_is_not_exposed(tmp_path: Path):
    root = valid_project(tmp_path)
    advance_to(root, "footage")
    write_waiting_route(root, "footage", actor="gemini", revision=2, packet_bytes=b"current")
    (root / "AGENT" / "gemini_handoff.pdf").write_bytes(b"older-or-changed")

    snapshot = inspect_operator_project(root)

    assert snapshot["status"] == "waiting_agent"
    assert snapshot["current_task"]["state"] == WAITING_GEMINI
    assert snapshot["portable_packet"] is None


def test_stale_route_revision_is_not_treated_as_current_wait(tmp_path: Path):
    root = valid_project(tmp_path)
    advance_to(root, "footage")
    write_waiting_route(root, "footage", actor="gemini", revision=3, packet_bytes=b"r3")
    request_path = root / "AGENT" / "request.json"
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request["route"]["revision"] = 2
    request_path.write_text(json.dumps(request), encoding="utf-8")

    snapshot = inspect_operator_project(root)

    assert snapshot["status"] == "ready"
    assert snapshot["portable_packet"] is None
    assert snapshot["route"] is None


def test_reopening_while_worker_waits_preserves_waiting_owner(tmp_path: Path):
    root = valid_project(tmp_path)
    advance_to(root, "analyze")
    write_waiting_route(root, "analyze", actor="gemini", packet_bytes=b"current")
    lock = ProjectRunLock(root)
    lock.acquire()
    try:
        snapshot = inspect_operator_project(root)
    finally:
        lock.release()

    assert snapshot["status"] == "waiting_agent"
    assert snapshot["worker_active"] is True
    assert snapshot["can_start"] is False
    assert snapshot["current_task"]["state"] == WAITING_GEMINI


def test_response_received_is_owned_by_moon(tmp_path: Path):
    root = valid_project(tmp_path)
    MoonProject.open(root, create=True)
    OperatorStatusStore(root).save(
        status="response_received",
        stage="proposal",
        message="response received",
    )
    lock = ProjectRunLock(root)
    lock.acquire()
    try:
        snapshot = inspect_operator_project(root)
    finally:
        lock.release()

    assert snapshot["current_task"]["state"] == RESPONSE_RECEIVED
    assert snapshot["current_task"]["owner"] == "MOON"


def test_operator_browser_urls_are_centrally_configurable(tmp_path: Path, monkeypatch):
    root = valid_project(tmp_path)
    moon = root / ".moon"
    moon.mkdir()
    (moon / "operator.json").write_text(
        json.dumps(
            {
                "gemini_url": "https://gemini.example/project",
                "chatgpt_url": "https://chatgpt.example/project",
            }
        ),
        encoding="utf-8",
    )

    configured = OperatorWebConfig.load(root)
    assert configured.gemini_url == "https://gemini.example/project"
    assert configured.chatgpt_url == "https://chatgpt.example/project"

    monkeypatch.setenv("MOON_OPERATOR_CHATGPT_URL", "https://chatgpt.example/admin")
    assert OperatorWebConfig.load(root).chatgpt_url == "https://chatgpt.example/admin"


def test_drive_response_is_detected_and_worker_auto_resumes(tmp_path: Path, monkeypatch):
    root = valid_project(tmp_path)
    runner = PipelineRunner(MoonProject.open(root, create=True))
    config = DriveBridgeConfig(
        project_id=root.name,
        transport="local_sync",
        sync_root=tmp_path / "drive",
        poll_interval_seconds=0.01,
    )

    class Bridge:
        def __init__(self, *_args, **_kwargs):
            self.polls = 0

        def publish(self, stage):
            return {
                "request": {
                    "request_id": "request-current",
                    "route": {"current_actor": "gpt", "revision": 0},
                }
            }

        def poll_once(self):
            self.polls += 1
            return None if self.polls == 1 else {"status": "CONSUMED"}

    monkeypatch.setattr(operator_module.DriveBridgeConfig, "load", lambda _root: config)
    monkeypatch.setattr(operator_module, "MoonDriveBridge", Bridge)
    worker = OperatorWorker(root, sleep=lambda _seconds: None)

    worker._wait_for_agent(runner, "proposal")
    durable = worker.store.load()

    assert durable["status"] == "response_received"
    assert durable["request_id"] == "request-current"
    assert "tự động tiếp tục" in durable["message"]


def test_transient_drive_error_retries_without_failing_worker(tmp_path: Path, monkeypatch):
    root = valid_project(tmp_path)
    runner = PipelineRunner(MoonProject.open(root, create=True))
    config = DriveBridgeConfig(
        project_id=root.name,
        transport="local_sync",
        sync_root=tmp_path / "drive",
        poll_interval_seconds=0.01,
    )

    class Bridge:
        def __init__(self, *_args, **_kwargs):
            self.polls = 0

        def publish(self, stage):
            return {
                "request": {
                    "request_id": "request-current",
                    "route": {"current_actor": "gpt", "revision": 0},
                }
            }

        def poll_once(self):
            self.polls += 1
            if self.polls == 1:
                raise BridgeTransportError("temporary Drive outage")
            return {"status": "CONSUMED"}

    monkeypatch.setattr(operator_module.DriveBridgeConfig, "load", lambda _root: config)
    monkeypatch.setattr(operator_module, "MoonDriveBridge", Bridge)
    worker = OperatorWorker(root, sleep=lambda _seconds: None)
    saves = []
    original_save = worker._save

    def recording_save(**fields):
        saves.append(fields)
        original_save(**fields)

    worker._save = recording_save

    worker._wait_for_agent(runner, "match")
    durable = worker.store.load()

    assert durable["status"] == "response_received"
    assert durable["transport_error"] is None
    assert any(
        item.get("transport_error") == "temporary Drive outage"
        and "tự động thử lại" in item.get("message", "")
        for item in saves
    )


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


def test_worker_auto_advances_multiple_footage_batches_without_manual_restart(
    tmp_path: Path, monkeypatch
):
    root = valid_project(tmp_path)
    runner = PipelineRunner(MoonProject.open(root, create=True))
    runner.state.completed = ["proposal", "analyze"]
    runner.state.status = "idle"
    runner.state.save(runner.project.state_path)
    next_calls = []

    class AgentBridge:
        def __init__(self, current_runner):
            self.runner = current_runner

        def next(self):
            stage = self.runner.state.next_stage()
            next_calls.append(stage)
            if stage == "match":
                self.runner.state.completed = list(DEFAULT_STAGES)
                self.runner.state.status = "complete"
                self.runner.state.current_stage = None
                self.runner.state.save(self.runner.project.state_path)
                final = root / "output" / "final.mp4"
                final.parent.mkdir(parents=True, exist_ok=True)
                final.write_bytes(b"final")
                return {"status": "complete"}
            return {"status": "awaiting_agent", "stage": "footage"}

    monkeypatch.setattr(operator_module, "AgentBridgeService", AgentBridge)
    worker = OperatorWorker(root, sleep=lambda _seconds: None)
    waits = []

    def wait_for_batch(current_runner, stage):
        waits.append(stage)
        if len(waits) == 2:
            current_runner.state.completed = ["proposal", "analyze", "footage"]
            current_runner.state.status = "idle"
            current_runner.state.current_stage = None
            current_runner.state.save(current_runner.project.state_path)

    worker._wait_for_agent = wait_for_batch

    assert worker._run_locked() == 0
    assert waits == ["footage", "footage"]
    assert next_calls == ["footage", "footage", "match"]

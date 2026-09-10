from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Callable

from moon.agent_bridge import AgentBridgeService
from moon.core.project import MoonProject
from moon.core.state import DEFAULT_STAGES, PipelineState
from moon.drive_bridge import (
    BridgeResponseError,
    BridgeTransportError,
    DriveBridgeConfig,
    MoonDriveBridge,
    REMOTE_ROOT_NAME,
)
from moon.media.inspection import VIDEO_EXTENSIONS
from moon.runner.pipeline import PipelineRunner


OPERATOR_VERSION = "1.0"
STAGE_LABELS = {
    "proposal": "Proposal",
    "analyze": "Analyze",
    "footage": "Footage",
    "match": "Match",
    "timeline": "Timeline",
    "render": "Render",
    "qc": "QC",
}
PENDING = "Pending"
READY = "Ready"
RUNNING = "Running"
WAITING_AGENT = "Waiting for Agent"
COMPLETED = "Completed"
FAILED = "Failed"
LOCAL_PROCESSING = "LOCAL_PROCESSING"
WAITING_CHATGPT = "WAITING_CHATGPT"
RESPONSE_RECEIVED = "RESPONSE_RECEIVED"
TASK_FAILED = "FAILED"
TASK_COMPLETE = "COMPLETE"
DEFAULT_CHATGPT_URL = "https://chatgpt.com/"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


@dataclass(frozen=True)
class ProjectValidation:
    valid: bool
    project_root: str
    reference_path: str
    footage_path: str
    footage_count: int
    errors: tuple[str, ...]


def validate_operator_project(project_root: str | Path) -> ProjectValidation:
    root = Path(project_root).expanduser().resolve()
    reference = root / "reference.mp4"
    footage = root / "footage"
    errors: list[str] = []
    if not root.is_dir():
        errors.append("Thư mục dự án không tồn tại.")
    if not reference.is_file():
        errors.append("Thiếu file reference.mp4 trong thư mục dự án.")
    if not footage.is_dir():
        errors.append("Thiếu thư mục footage trong thư mục dự án.")
        videos: list[Path] = []
    else:
        videos = [
            path
            for path in footage.rglob("*")
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
        ]
        if not videos:
            errors.append("Thư mục footage chưa có video được hỗ trợ.")
    return ProjectValidation(
        valid=not errors,
        project_root=str(root),
        reference_path=str(reference),
        footage_path=str(footage),
        footage_count=len(videos),
        errors=tuple(errors),
    )


def stage_status_mapping(
    state: PipelineState,
    *,
    activity: str | None = None,
    activity_stage: str | None = None,
    worker_active: bool = False,
) -> list[dict[str, str]]:
    statuses: list[dict[str, str]] = []
    next_stage = state.next_stage()
    for stage in DEFAULT_STAGES:
        status = COMPLETED if stage in state.completed else PENDING
        if stage not in state.completed:
            if activity_stage == stage and activity == "waiting_agent":
                status = WAITING_AGENT
            elif activity_stage == stage and activity == "failed":
                status = FAILED
            elif (
                activity_stage == stage
                and activity == "running"
                and worker_active
            ):
                status = RUNNING
            elif state.status == "blocked" and stage == (state.current_stage or next_stage):
                status = FAILED
            elif (
                worker_active
                and state.status == "running"
                and stage == (state.current_stage or next_stage)
            ):
                status = RUNNING
            elif (
                not worker_active
                and stage == (state.current_stage or next_stage)
                and state.status == "running"
            ):
                status = READY
        statuses.append(
            {"stage": stage, "label": STAGE_LABELS[stage], "status": status}
        )
    return statuses


@dataclass(frozen=True)
class OperatorWebConfig:
    chatgpt_url: str = DEFAULT_CHATGPT_URL

    @classmethod
    def load(cls, project_root: str | Path | None = None) -> OperatorWebConfig:
        payload: dict[str, Any] = {}
        if project_root is not None:
            payload = _safe_read_json(
                Path(project_root).expanduser().resolve() / ".moon" / "operator.json"
            )
        return cls(
            chatgpt_url=str(
                os.environ.get("MOON_OPERATOR_CHATGPT_URL")
                or payload.get("chatgpt_url")
                or DEFAULT_CHATGPT_URL
            ),
        )


def chatgpt_handoff_instruction(project_root: str | Path, stage: str) -> str:
    root = Path(project_root).expanduser().resolve()
    project = str(
        _safe_read_json(root / ".moon" / "bridge.json").get("project_id")
        or root.name
    )
    return (
        f"Tôi đang vận hành AI Video Replicator project {project}.\n"
        "Hãy đọc request.json hiện tại và toàn bộ evidence được request tham chiếu "
        "trong thư mục Google Drive AGENT.\n"
        f"Thực hiện đúng contract của stage {stage}.\n"
        "Sau khi hoàn thành, ghi raw application/json response.json trở lại đúng "
        "thư mục AGENT.\n"
        "Giữ nguyên job_id, request_id, stage, revision.\n"
        "Không yêu cầu tôi copy/paste JSON thủ công."
    )


class OperatorStatusStore:
    def __init__(self, project_root: str | Path) -> None:
        self.root = Path(project_root).expanduser().resolve()
        self.path = self.root / ".moon" / "operator-status.json"
        self.stdout_path = self.root / ".moon" / "operator-stdout.log"
        self.stderr_path = self.root / ".moon" / "operator-stderr.log"

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def save(self, **fields: Any) -> dict[str, Any]:
        current = self.load()
        current.update(
            version=OPERATOR_VERSION,
            project_root=str(self.root),
            updated_at=_utc_now(),
            **fields,
        )
        _atomic_json(self.path, current)
        return current

    @staticmethod
    def tail(path: Path, *, max_chars: int = 24000) -> str:
        if not path.is_file():
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-max_chars:]


class DuplicateProjectRun(RuntimeError):
    pass


class ProjectRunLock:
    def __init__(self, project_root: str | Path) -> None:
        self.root = Path(project_root).expanduser().resolve()
        self.path = self.root / ".moon" / "operator-run.lock"
        self.token = uuid.uuid4().hex
        self.owned = False

    @staticmethod
    def _pid_running(pid: int) -> bool:
        if pid <= 0:
            return False
        if pid == os.getpid():
            return True
        if os.name == "nt":
            import ctypes

            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
                process_query_limited_information, False, pid
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
                return True
            return ctypes.get_last_error() == 5  # access denied still means it exists
        try:
            os.kill(pid, 0)
        except (OSError, ValueError):
            return False
        return True

    def active_owner(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        try:
            pid = int(payload.get("pid", 0))
        except (TypeError, ValueError):
            return None
        return payload if self._pid_running(pid) else None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            payload = {
                "version": OPERATOR_VERSION,
                "pid": os.getpid(),
                "token": self.token,
                "project_root": str(self.root),
                "started_at": _utc_now(),
            }
            try:
                descriptor = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
            except FileExistsError:
                if self.active_owner() is not None:
                    raise DuplicateProjectRun(
                        "Dự án này đang được AI Video Replicator xử lý."
                    )
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            self.owned = True
            return
        raise DuplicateProjectRun("Không thể tạo khóa chạy cho dự án này.")

    def release(self) -> None:
        if not self.owned:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("token") == self.token:
                self.path.unlink()
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            pass
        self.owned = False

    def __enter__(self) -> ProjectRunLock:
        self.acquire()
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()


def _safe_read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _drive_exchange_details(
    root: Path, *, job_id: str | None = None
) -> tuple[str | None, str | None]:
    payload = _safe_read_json(root / ".moon" / "bridge.json")
    project_id = str(job_id or payload.get("project_id") or root.name)
    remote_path = f"{REMOTE_ROOT_NAME}/jobs/{project_id}/AGENT"
    drive = payload.get("drive") or {}
    if payload.get("transport", "google_drive_api") != "local_sync" or not isinstance(drive, dict):
        return remote_path, None
    sync_root = drive.get("sync_root")
    if not isinstance(sync_root, str) or not sync_root.strip():
        return remote_path, None
    folder = Path(os.path.expandvars(sync_root)).expanduser().resolve() / remote_path
    return remote_path, str(folder)


def _identity_matches(
    value: dict[str, Any], active: dict[str, Any], *, next_stage: str
) -> bool:
    try:
        value_revision = int(value.get("revision"))
        active_revision = int(active.get("revision"))
    except (TypeError, ValueError):
        return False
    if (
        value.get("request_id") != active.get("request_id")
        or value.get("stage") != active.get("stage")
        or value.get("stage") != next_stage
        or value_revision != active_revision
    ):
        return False
    active_job = active.get("job_id")
    return active_job is None or value.get("job_id") == active_job


def _request_identity_matches(
    request: dict[str, Any], active: dict[str, Any], *, next_stage: str
) -> bool:
    if (
        request.get("request_id") != active.get("request_id")
        or request.get("stage") != active.get("stage")
        or request.get("stage") != next_stage
    ):
        return False
    active_job = active.get("job_id")
    return active_job is None or request.get("job_id") == active_job


def _canonical_waiting_route(
    root: Path, *, next_stage: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    """Read the current published route only when all identities agree."""

    bridge_state = _safe_read_json(root / ".moon" / "bridge-state.json")
    active = bridge_state.get("active_request")
    request = _safe_read_json(root / "AGENT" / "request.json")
    route = request.get("route")
    if (
        not isinstance(active, dict)
        or active.get("status") != "WAITING_AGENT"
        or not isinstance(route, dict)
        or request.get("status") != "WAITING_AGENT"
        or not _request_identity_matches(request, active, next_stage=next_stage)
        or not _identity_matches(route, active, next_stage=next_stage)
        or route.get("current_actor") != "gpt"
        or route.get("status") != "WAITING_GPT"
    ):
        return None
    # agent-state is a reconstructible cache. A stale cache must not override the
    # canonical route embedded in request.json.
    agent_state = _safe_read_json(root / ".moon" / "agent-state.json")
    if agent_state and not _identity_matches(
        agent_state, active, next_stage=next_stage
    ):
        agent_state = {}
    return active, request, route


def _task_snapshot(
    *,
    stage: str | None,
    task_state: str,
    owner: str,
    title: str,
    detail: str,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "stage_label": STAGE_LABELS.get(stage or "", stage or "-"),
        "state": task_state,
        "owner": owner,
        "title": title,
        "detail": detail,
    }


def inspect_operator_project(project_root: str | Path) -> dict[str, Any]:
    validation = validate_operator_project(project_root)
    root = Path(validation.project_root)
    store = OperatorStatusStore(root)
    durable = store.load()
    lock_active = ProjectRunLock(root).active_owner() is not None
    remote_path, drive_folder = _drive_exchange_details(root)
    debug = {
        "commands": list(durable.get("commands") or []),
        "stdout": store.tail(store.stdout_path),
        "stderr": store.tail(store.stderr_path),
        "request_id": durable.get("request_id"),
        "stage": durable.get("stage"),
        "revision": durable.get("revision"),
        "owner": durable.get("owner") or "moon",
        "remote_path": durable.get("remote_path") or remote_path,
        "stage_internals": str(durable.get("stage_internals") or ""),
    }
    if not validation.valid:
        state = PipelineState()
        detail = "\n".join(validation.errors)
        return {
            "status": "invalid",
            "message": detail,
            "validation": asdict(validation),
            "stages": stage_status_mapping(state),
            "project_root": str(root),
            "final_path": str(root / "output" / "final.mp4"),
            "drive_folder": drive_folder,
            "current_task": _task_snapshot(
                stage=None,
                task_state=TASK_FAILED,
                owner="MOON",
                title="DỰ ÁN CHƯA SẴN SÀNG",
                detail=detail,
            ),
            "route": None,
            "can_start": False,
            "worker_active": False,
            "debug": debug,
        }

    state_path = root / ".moon" / "state.json"
    state = PipelineState.load(state_path) if state_path.is_file() else PipelineState()
    final_path = root / "output" / "final.mp4"
    if state.next_stage() is None:
        complete = final_path.is_file()
        detail = (
            "Video đã hoàn tất."
            if complete
            else "Pipeline đã hoàn tất nhưng không tìm thấy output/final.mp4."
        )
        return {
            "status": "complete" if complete else "failed",
            "message": detail,
            "validation": asdict(validation),
            "stages": stage_status_mapping(
                state,
                activity="failed" if not complete else None,
                activity_stage="qc",
                worker_active=lock_active,
            ),
            "project_root": str(root),
            "final_path": str(final_path),
            "drive_folder": drive_folder,
            "current_task": _task_snapshot(
                stage="qc",
                task_state=TASK_COMPLETE if complete else TASK_FAILED,
                owner="MOON",
                title="HOÀN TẤT" if complete else "THIẾU VIDEO ĐẦU RA",
                detail=detail,
            ),
            "route": None,
            "can_start": False,
            "worker_active": lock_active,
            "debug": debug,
        }

    next_stage = state.next_stage()
    assert next_stage is not None
    routed = _canonical_waiting_route(root, next_stage=next_stage)
    active, _request, route = routed if routed else ({}, {}, {})
    waiting = routed is not None
    remote_path, drive_folder = _drive_exchange_details(
        root, job_id=str(active.get("job_id") or "") or None
    )
    durable_status = str(durable.get("status") or "")
    durable_stage = str(durable.get("stage") or "")
    if durable_stage not in DEFAULT_STAGES or durable_stage in state.completed:
        durable_stage = next_stage

    if durable_status == "failed" and not lock_active:
        activity, activity_stage = "failed", durable_stage
        overall = "failed"
        message = str(durable.get("message") or "Không thể tiếp tục xử lý dự án.")
        current_task = _task_snapshot(
            stage=activity_stage,
            task_state=TASK_FAILED,
            owner="MOON",
            title="XỬ LÝ THẤT BẠI",
            detail=message,
        )
    elif waiting:
        activity, activity_stage = "waiting_agent", next_stage
        overall = "waiting_agent"
        task_state = WAITING_CHATGPT
        owner = "GPT"
        title = "CẦN GPT PHÂN TÍCH"
        detail = (
            "Mở ChatGPT và dùng yêu cầu ngắn để xử lý request cùng evidence "
            "trong thư mục Drive AGENT. Moon sẽ tự động nhận response.json hợp lệ."
        )
        if (
            durable.get("transport_error")
            and durable.get("request_id") == active.get("request_id")
        ):
            detail = "Mất kết nối Google Drive tạm thời. Moon đang tự động thử lại..."
        message = detail
        current_task = _task_snapshot(
            stage=next_stage,
            task_state=task_state,
            owner=owner,
            title=title,
            detail=detail,
        )
    elif lock_active:
        activity, activity_stage = "running", durable_stage
        overall = "running"
        response_received = durable_status == "response_received"
        message = str(durable.get("message") or "Đang xử lý video...")
        current_task = _task_snapshot(
            stage=activity_stage,
            task_state=RESPONSE_RECEIVED if response_received else LOCAL_PROCESSING,
            owner="MOON",
            title="ĐÃ NHẬN PHẢN HỒI" if response_received else "ĐANG PHÂN TÍCH LOCAL",
            detail=(
                "Moon đã nhận phản hồi và đang tiếp tục pipeline."
                if response_received
                else "Đang xử lý tự động..."
            ),
        )
    elif state.status == "blocked":
        activity, activity_stage = "failed", state.current_stage or next_stage
        overall = "failed"
        message = "Bước xử lý hiện tại đang bị lỗi. Nhấn START AI EDIT để thử lại."
        current_task = _task_snapshot(
            stage=activity_stage,
            task_state=TASK_FAILED,
            owner="MOON",
            title="XỬ LÝ THẤT BẠI",
            detail=message,
        )
    else:
        activity, activity_stage = None, None
        overall = "ready"
        message = "Sẵn sàng tiếp tục từ trạng thái đã lưu."
        current_task = _task_snapshot(
            stage=next_stage,
            task_state=LOCAL_PROCESSING,
            owner="MOON",
            title="SẴN SÀNG TIẾP TỤC",
            detail=message,
        )

    return {
        "status": overall,
        "message": message,
        "validation": asdict(validation),
        "stages": stage_status_mapping(
            state,
            activity=activity,
            activity_stage=activity_stage,
            worker_active=lock_active,
        ),
        "stage": activity_stage or next_stage,
        "project_root": str(root),
        "final_path": str(final_path),
        "drive_folder": drive_folder,
        "current_task": current_task,
        "route": {
            "current_actor": route.get("current_actor"),
            "next_actor": route.get("next_actor"),
            "next_action": route.get("next_action"),
            "revision": route.get("revision"),
        }
        if waiting
        else None,
        "can_start": not lock_active,
        "worker_active": lock_active,
        "debug": {
            **debug,
            "request_id": active.get("request_id") or debug["request_id"],
            "stage": activity_stage or next_stage,
            "revision": route.get("revision") if waiting else state.revision,
            "owner": route.get("current_actor") if waiting else "moon",
            "remote_path": remote_path,
            "stage_internals": (
                f"pipeline_status={state.status}; next_stage={next_stage}; "
                f"pipeline_revision={state.revision}; worker_active={lock_active}; "
                f"route_actor={route.get('current_actor') if waiting else '-'}"
            ),
        },
    }


class OperatorWorker:
    def __init__(
        self,
        project_root: str | Path,
        *,
        stop_event: Event | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.root = Path(project_root).expanduser().resolve()
        self.stop_event = stop_event or Event()
        self.sleep = sleep
        self.store = OperatorStatusStore(self.root)
        self.commands: list[str] = [
            f'"{sys.executable}" -m moon.operator_worker "{self.root}"'
        ]

    def _command(self, value: str) -> None:
        self.commands.append(value)
        self.commands = self.commands[-100:]

    def _save(self, **fields: Any) -> None:
        self.store.save(pid=os.getpid(), commands=self.commands, **fields)

    def run(self) -> int:
        validation = validate_operator_project(self.root)
        if not validation.valid:
            self._save(status="failed", stage=None, message="\n".join(validation.errors))
            return 2
        PipelineRunner(MoonProject.open(self.root, create=True))
        lock = ProjectRunLock(self.root)
        try:
            lock.acquire()
        except DuplicateProjectRun as exc:
            print(str(exc), file=sys.stderr, flush=True)
            return 3
        try:
            self._save(
                status="running",
                stage=None,
                message="Đang đọc trạng thái dự án...",
                request_id=None,
            )
            return self._run_locked()
        except Exception as exc:  # noqa: BLE001 -- worker must leave a durable operator error
            stage = PipelineRunner(MoonProject.open(self.root)).state.next_stage()
            self._save(
                status="failed",
                stage=stage,
                message=self._human_error(exc, stage),
                error_type=type(exc).__name__,
            )
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return 1
        finally:
            lock.release()

    def _run_locked(self) -> int:
        while not self.stop_event.is_set():
            runner = PipelineRunner(MoonProject.open(self.root))
            stage = runner.state.next_stage()
            if stage is None:
                final = self.root / "output" / "final.mp4"
                if not final.is_file():
                    self._save(
                        status="failed",
                        stage="qc",
                        message="Pipeline đã hoàn tất nhưng không tìm thấy output/final.mp4.",
                    )
                    return 1
                self._save(
                    status="complete",
                    stage=None,
                    message="Video đã hoàn tất.",
                    final_path=str(final),
                    request_id=None,
                )
                return 0

            self._command(
                f'"{sys.executable}" -m moon --project "{self.root}" next'
            )
            self._save(
                status="running",
                stage=stage,
                message=f"Đang xử lý bước {STAGE_LABELS[stage]}...",
                request_id=None,
                stage_internals=f"Moon next: {stage}",
            )
            result = AgentBridgeService(runner).next()
            status = result.get("status")
            if status == "complete":
                continue
            if status == "blocked":
                detail = (result.get("result") or {}).get("error") or "Moon reported a blocked stage"
                raise RuntimeError(str(detail))
            if status != "awaiting_agent":
                raise RuntimeError(f"Moon returned unexpected status {status!r}")
            self._wait_for_agent(runner, str(result["stage"]))
        return 0

    def _wait_for_agent(self, runner: PipelineRunner, stage: str) -> None:
        config = DriveBridgeConfig.load(self.root)
        bridge = MoonDriveBridge(runner, config)
        self._command(
            f'"{sys.executable}" -m moon bridge publish "{self.root}" {stage}'
        )
        published: dict[str, Any] | None = None
        while not self.stop_event.is_set():
            try:
                published = bridge.publish(stage)
                break
            except BridgeTransportError as exc:
                self._save(
                    status="running",
                    stage=stage,
                    message="Mất kết nối Google Drive tạm thời. Đang tự động thử lại...",
                    request_id=None,
                    transport_error=str(exc),
                    owner="moon",
                    revision=runner.state.revision,
                    remote_path=config.remote_path,
                )
                self.sleep(config.poll_interval_seconds)
        if published is None:
            return
        request = published["request"]
        route = request.get("route") or {}
        self._save(
            status="waiting_agent",
            stage=stage,
            message=f"Cần GPT phân tích bước {STAGE_LABELS[stage]}...",
            request_id=request.get("request_id"),
            transport_error=None,
            owner="gpt",
            revision=route.get("revision"),
            remote_path=config.remote_path,
            stage_internals=(
                f"bridge_status=WAITING_AGENT; stage={stage}; "
                f"revision={(request.get('route') or {}).get('revision')}"
            ),
        )
        self._command(
            f'"{sys.executable}" -m moon bridge watch "{self.root}"'
        )
        while not self.stop_event.is_set():
            try:
                consumed = bridge.poll_once()
            except BridgeTransportError as exc:
                self._save(
                    status="waiting_agent",
                    stage=stage,
                    message="Mất kết nối Google Drive tạm thời. Đang tự động thử lại...",
                    request_id=request.get("request_id"),
                    transport_error=str(exc),
                    owner="gpt",
                    revision=route.get("revision"),
                    remote_path=config.remote_path,
                )
                self.sleep(config.poll_interval_seconds)
                continue
            except BridgeResponseError:
                raise
            if self.store.load().get("transport_error"):
                self._save(
                    status="waiting_agent",
                    stage=stage,
                    message=f"Cần GPT phân tích bước {STAGE_LABELS[stage]}...",
                    request_id=request.get("request_id"),
                    transport_error=None,
                    owner="gpt",
                    revision=route.get("revision"),
                    remote_path=config.remote_path,
                )
            if consumed is not None:
                self._save(
                    status="response_received",
                    stage=stage,
                    message="Đã nhận phản hồi hợp lệ. Moon đang tự động tiếp tục pipeline...",
                    request_id=request.get("request_id"),
                    transport_error=None,
                    owner="moon",
                    revision=route.get("revision"),
                    remote_path=config.remote_path,
                    stage_internals=(
                        f"bridge_status={consumed.get('status')}; stage={stage}; "
                        f"revision={route.get('revision')}"
                    ),
                )
                return
            self.sleep(config.poll_interval_seconds)

    @staticmethod
    def _human_error(exc: Exception, stage: str | None) -> str:
        if isinstance(exc, FileNotFoundError) and "bridge.json" in str(exc):
            return (
                "Chưa có cấu hình kết nối Google Drive cho dự án. "
                "Hãy nhờ người cài đặt cấu hình một lần rồi nhấn START AI EDIT."
            )
        if isinstance(exc, BridgeResponseError):
            return (
                "Phản hồi từ trợ lý AI không hợp lệ hoặc đã cũ. "
                "Hãy để GPT sửa response.json theo request Drive hiện tại rồi nhấn START AI EDIT."
            )
        if isinstance(exc, BridgeTransportError):
            return "Không thể kết nối Google Drive. Hãy kiểm tra mạng rồi thử lại."
        label = STAGE_LABELS.get(stage or "", stage or "hiện tại")
        return f"Không thể hoàn tất bước {label}. Mở Chi tiết kỹ thuật để xem lỗi."


class WorkerLaunchError(RuntimeError):
    pass


class OperatorWorkerProcess:
    """Launch the durable Moon worker with the repository venv Python."""

    def __init__(
        self,
        repository_root: str | Path,
        *,
        popen: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
    ) -> None:
        self.repository_root = Path(repository_root).expanduser().resolve()
        self.popen = popen
        self.process: subprocess.Popen[Any] | None = None

    @property
    def python_path(self) -> Path:
        windows = self.repository_root / ".venv" / "Scripts" / "python.exe"
        if windows.is_file():
            return windows
        posix = self.repository_root / ".venv" / "bin" / "python"
        if posix.is_file():
            return posix
        raise WorkerLaunchError(
            "Không tìm thấy Python của dự án (.venv). Hãy nhờ người cài đặt kiểm tra."
        )

    def start(self, project_root: str | Path) -> subprocess.Popen[Any]:
        root = Path(project_root).expanduser().resolve()
        PipelineRunner(MoonProject.open(root, create=True))
        if ProjectRunLock(root).active_owner() is not None:
            raise DuplicateProjectRun(
                "Dự án này đang được AI Video Replicator xử lý."
            )
        store = OperatorStatusStore(root)
        store.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        command = [str(self.python_path), "-m", "moon.operator_worker", str(root)]
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            with store.stdout_path.open("a", encoding="utf-8") as stdout_handle, store.stderr_path.open(
                "a", encoding="utf-8"
            ) as stderr_handle:
                self.process = self.popen(
                    command,
                    cwd=str(self.repository_root),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    creationflags=creationflags,
                    start_new_session=os.name != "nt",
                )
        except OSError as exc:
            raise WorkerLaunchError(
                "Không thể khởi động Moon. Hãy nhờ người cài đặt kiểm tra .venv."
            ) from exc
        return self.process

    def failure(self) -> str | None:
        if self.process is None:
            return None
        code = self.process.poll()
        if code in (None, 0):
            return None
        return f"Moon worker stopped unexpectedly (exit code {code})."

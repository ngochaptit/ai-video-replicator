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
RUNNING = "Running"
WAITING_AGENT = "Waiting for Agent"
COMPLETED = "Completed"
FAILED = "Failed"
VISUAL_AGENT_STAGES = {"analyze", "footage"}


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
            elif activity_stage == stage and activity == "running":
                status = RUNNING
            elif state.status == "blocked" and stage == (state.current_stage or next_stage):
                status = FAILED
            elif state.status == "running" and stage == (state.current_stage or next_stage):
                status = RUNNING
        statuses.append(
            {"stage": stage, "label": STAGE_LABELS[stage], "status": status}
        )
    return statuses


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


def inspect_operator_project(project_root: str | Path) -> dict[str, Any]:
    validation = validate_operator_project(project_root)
    root = Path(validation.project_root)
    store = OperatorStatusStore(root)
    durable = store.load()
    lock_active = ProjectRunLock(root).active_owner() is not None
    debug = {
        "commands": list(durable.get("commands") or []),
        "stdout": store.tail(store.stdout_path),
        "stderr": store.tail(store.stderr_path),
        "request_id": durable.get("request_id"),
        "stage_internals": str(durable.get("stage_internals") or ""),
    }
    if not validation.valid:
        state = PipelineState()
        return {
            "status": "invalid",
            "message": "\n".join(validation.errors),
            "validation": asdict(validation),
            "stages": stage_status_mapping(state),
            "project_root": str(root),
            "final_path": str(root / "output" / "final.mp4"),
            "portable_packet": None,
            "can_start": False,
            "worker_active": False,
            "debug": debug,
        }

    state_path = root / ".moon" / "state.json"
    state = PipelineState.load(state_path) if state_path.is_file() else PipelineState()
    final_path = root / "output" / "final.mp4"
    if state.next_stage() is None:
        complete = final_path.is_file()
        return {
            "status": "complete" if complete else "failed",
            "message": (
                "Video đã hoàn tất."
                if complete
                else "Pipeline đã hoàn tất nhưng không tìm thấy output/final.mp4."
            ),
            "validation": asdict(validation),
            "stages": stage_status_mapping(
                state, activity="failed" if not complete else None, activity_stage="qc"
            ),
            "project_root": str(root),
            "final_path": str(final_path),
            "portable_packet": None,
            "can_start": False,
            "worker_active": lock_active,
            "debug": debug,
        }

    next_stage = state.next_stage()
    bridge_state = _safe_read_json(root / ".moon" / "bridge-state.json")
    active = bridge_state.get("active_request") or {}
    waiting = (
        isinstance(active, dict)
        and active.get("status") == "WAITING_AGENT"
        and active.get("stage") == next_stage
    )
    durable_status = str(durable.get("status") or "")
    durable_stage = str(durable.get("stage") or "") or next_stage
    if durable_status == "failed" and not lock_active:
        activity, activity_stage = "failed", durable_stage
        overall = "failed"
        message = str(durable.get("message") or "Không thể tiếp tục xử lý dự án.")
    elif waiting:
        activity, activity_stage = "waiting_agent", next_stage
        overall = "waiting_agent"
        message = str(durable.get("message") or "Đang chờ trợ lý AI hoàn tất.")
    elif lock_active:
        activity, activity_stage = "running", durable_stage
        overall = "running"
        message = str(durable.get("message") or "Đang xử lý video...")
    elif state.status == "blocked":
        activity, activity_stage = "failed", state.current_stage or next_stage
        overall = "failed"
        message = "Bước xử lý hiện tại đang bị lỗi. Nhấn START AI EDIT để thử lại."
    else:
        activity, activity_stage = None, None
        overall = "ready"
        message = "Dự án sẵn sàng tiếp tục từ trạng thái đã lưu."

    portable_packet: str | None = None
    agent_state = _safe_read_json(root / ".moon" / "agent-state.json")
    request = _safe_read_json(root / "AGENT" / "request.json")
    candidate = root / "AGENT" / "gemini_handoff.pdf"
    if (
        waiting
        and next_stage in VISUAL_AGENT_STAGES
        and candidate.is_file()
        and agent_state.get("current_actor") == "gemini"
        and request.get("request_id") == active.get("request_id")
        and (request.get("route") or {}).get("revision") == active.get("revision")
    ):
        portable_packet = str(candidate)

    return {
        "status": overall,
        "message": message,
        "validation": asdict(validation),
        "stages": stage_status_mapping(
            state, activity=activity, activity_stage=activity_stage
        ),
        "stage": activity_stage or next_stage,
        "project_root": str(root),
        "final_path": str(final_path),
        "portable_packet": portable_packet,
        "can_start": not lock_active,
        "worker_active": lock_active,
        "debug": {
            **debug,
            "request_id": active.get("request_id") or debug["request_id"],
            "stage_internals": (
                f"pipeline_status={state.status}; next_stage={next_stage}; "
                f"pipeline_revision={state.revision}; worker_active={lock_active}"
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
        published = bridge.publish(stage)
        request = published["request"]
        packet = self.root / "AGENT" / "gemini_handoff.pdf"
        portable = str(packet) if stage in VISUAL_AGENT_STAGES and packet.is_file() else None
        self._save(
            status="waiting_agent",
            stage=stage,
            message=(
                "Cần phân tích bằng Gemini"
                if portable
                else f"Đang chờ trợ lý AI hoàn tất bước {STAGE_LABELS[stage]}..."
            ),
            request_id=request.get("request_id"),
            portable_packet=portable,
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
                    portable_packet=portable,
                    transport_error=str(exc),
                )
                self.sleep(config.poll_interval_seconds)
                continue
            except BridgeResponseError:
                raise
            if consumed is not None:
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
                "Hãy gửi lại đúng gói Gemini hiện tại rồi nhấn START AI EDIT."
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

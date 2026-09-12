from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from moon.atomic import atomic_write_json
from moon.drive_bridge import (
    BridgeResponseError,
    BridgeTransportError,
    DriveBridgeConfig,
    MoonDriveBridge,
)
from moon.operator import (
    OperatorStatusStore,
    OperatorWebConfig,
    OperatorWorker,
    STAGE_LABELS,
    chatgpt_handoff_instruction,
)


@dataclass(frozen=True)
class AutoGPTConfig:
    enabled: bool = True
    executable: str = "interpreter"
    profile: str | None = None
    timeout_seconds: int = 180
    max_dispatch_attempts: int = 2

    @classmethod
    def load(cls, project_root: str | Path) -> "AutoGPTConfig":
        root = Path(project_root).expanduser().resolve()
        payload: dict[str, Any] = {}
        path = root / ".moon" / "operator.json"
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    payload = value
            except (OSError, UnicodeError, json.JSONDecodeError):
                payload = {}

        auto = payload.get("auto_chatgpt")
        if not isinstance(auto, dict):
            auto = {}

        env_enabled = os.environ.get("MOON_OPERATOR_AUTO_CHATGPT")
        if env_enabled is not None:
            enabled = env_enabled.strip().lower() not in {"0", "false", "no", "off"}
        elif "enabled" in auto:
            enabled = bool(auto.get("enabled"))
        else:
            enabled = os.name == "nt"

        executable = str(
            os.environ.get("MOON_OPERATOR_INTERPRETER")
            or auto.get("executable")
            or "interpreter"
        )
        profile_value = (
            os.environ.get("MOON_OPERATOR_INTERPRETER_PROFILE")
            or auto.get("profile")
        )
        profile = str(profile_value).strip() if profile_value else None

        try:
            timeout_seconds = max(30, int(auto.get("timeout_seconds", 180)))
        except (TypeError, ValueError):
            timeout_seconds = 180
        try:
            max_attempts = max(1, int(auto.get("max_dispatch_attempts", 2)))
        except (TypeError, ValueError):
            max_attempts = 2

        return cls(
            enabled=enabled,
            executable=executable,
            profile=profile,
            timeout_seconds=timeout_seconds,
            max_dispatch_attempts=max_attempts,
        )


class AutoGPTDispatchStore:
    def __init__(self, project_root: str | Path) -> None:
        self.path = (
            Path(project_root).expanduser().resolve()
            / ".moon"
            / "auto-gpt-dispatch.json"
        )

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def save(self, payload: dict[str, Any]) -> None:
        atomic_write_json(self.path, payload)

    def attempts_for(self, request_id: str) -> int:
        value = self.load()
        if value.get("request_id") != request_id:
            return 0
        try:
            return int(value.get("attempts", 0))
        except (TypeError, ValueError):
            return 0

    def record(
        self,
        *,
        request_id: str,
        revision: int | None,
        attempt: int,
        success: bool,
        detail: str,
    ) -> None:
        self.save(
            {
                "version": "1.0",
                "request_id": request_id,
                "revision": revision,
                "attempts": attempt,
                "success": success,
                "detail": detail,
                "updated_at_epoch": time.time(),
            }
        )


class LocalGPTDesktopOperator:
    """Use Open Interpreter as a narrow local computer-use dispatcher.

    This agent is deliberately not allowed to analyze footage or edit project data.
    Its only job is to operate the already-authenticated ChatGPT UI and submit one
    handoff instruction. Moon and GPT keep their existing responsibilities.
    """

    def __init__(self, project_root: str | Path, config: AutoGPTConfig) -> None:
        self.root = Path(project_root).expanduser().resolve()
        self.config = config
        self.dispatch_store = AutoGPTDispatchStore(self.root)

    def available(self) -> bool:
        if not self.config.enabled:
            return False
        executable = self.config.executable
        if Path(executable).is_file():
            return True
        return shutil.which(executable) is not None

    def dispatch(
        self,
        *,
        stage: str,
        request_id: str,
        revision: int | None,
        force: bool = False,
        response_error: str | None = None,
    ) -> tuple[bool, str]:
        if not self.config.enabled:
            return False, "auto GPT operator disabled"
        if not self.available():
            return False, (
                "Open Interpreter chưa được cài hoặc không nằm trong PATH; "
                "giữ chế độ handoff thủ công."
            )

        previous_attempts = self.dispatch_store.attempts_for(request_id)
        if previous_attempts >= self.config.max_dispatch_attempts and not force:
            return False, "auto GPT operator đã đạt giới hạn dispatch cho request này"
        if previous_attempts > 0 and not force:
            return True, "request đã được auto-dispatch trước đó"

        attempt = previous_attempts + 1
        instruction = chatgpt_handoff_instruction(self.root, stage)
        if response_error:
            instruction += (
                "\n\nMoon vừa từ chối response.json hiện tại vì lỗi contract sau:\n"
                f"{response_error}\n"
                "Hãy đọc lại request.json HIỆN TẠI trên Drive, sửa response.json theo "
                "đúng request_id/revision/batch_id hiện tại, không phân tích lại evidence "
                "nếu semantic result cũ vẫn dùng được."
            )

        chatgpt_url = OperatorWebConfig.load(self.root).chatgpt_url
        prompt = (
            "Bạn là LOCAL OPERATOR của MON EDIT. Chỉ thao tác giao diện máy tính; "
            "không phân tích video, không sửa project JSON, không tự thay Moon hoặc GPT.\n"
            "Dùng computer-use/playwright để điều khiển trình duyệt thật của người dùng. "
            "Mở ChatGPT ở URL dưới đây trong phiên người dùng đã đăng nhập. "
            "Không đăng nhập, không nhập mật khẩu, không thay đổi tài khoản.\n"
            f"URL: {chatgpt_url}\n"
            "Tạo một chat mới nếu cần, dán NGUYÊN VĂN nội dung HANDOFF bên dưới vào "
            "ô soạn thảo và gửi đúng MỘT lần. Sau khi xác nhận message đã được gửi, "
            "dừng ngay; không chờ ChatGPT trả lời và không làm việc gì khác.\n\n"
            "--- HANDOFF START ---\n"
            f"{instruction}\n"
            "--- HANDOFF END ---"
        )

        command = [
            self.config.executable,
            "exec",
            "--ephemeral",
            "--sandbox",
            "danger-full-access",
            "--ask-for-approval",
            "never",
            "--timeout",
            str(self.config.timeout_seconds),
        ]
        if self.config.profile:
            command.extend(["--profile", self.config.profile])
        command.append("-")

        try:
            result = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                cwd=str(self.root),
                timeout=self.config.timeout_seconds + 15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            detail = f"auto GPT dispatch failed: {exc}"
            self.dispatch_store.record(
                request_id=request_id,
                revision=revision,
                attempt=attempt,
                success=False,
                detail=detail,
            )
            return False, detail

        detail = (result.stdout or result.stderr or "").strip()[-2000:]
        success = result.returncode == 0
        self.dispatch_store.record(
            request_id=request_id,
            revision=revision,
            attempt=attempt,
            success=success,
            detail=detail,
        )
        if success:
            return True, "Đã giao request cho local computer operator gửi sang ChatGPT."
        return False, detail or f"interpreter exit code {result.returncode}"


class AutoOperatorWorker(OperatorWorker):
    """Operator worker that auto-dispatches GPT handoffs but keeps manual fallback."""

    def __init__(self, project_root: str | Path, **kwargs: Any) -> None:
        super().__init__(project_root, **kwargs)
        self.auto_config = AutoGPTConfig.load(self.root)
        self.desktop_operator = LocalGPTDesktopOperator(self.root, self.auto_config)

    def _dispatch_gpt(
        self,
        *,
        stage: str,
        request: dict[str, Any],
        route: dict[str, Any],
        force: bool = False,
        response_error: str | None = None,
    ) -> None:
        request_id = str(request.get("request_id") or "")
        if not request_id:
            return
        revision_raw = route.get("revision")
        try:
            revision = int(revision_raw) if revision_raw is not None else None
        except (TypeError, ValueError):
            revision = None

        ok, detail = self.desktop_operator.dispatch(
            stage=stage,
            request_id=request_id,
            revision=revision,
            force=force,
            response_error=response_error,
        )
        if ok:
            self._save(
                status="waiting_agent",
                stage=stage,
                message="Local Operator đã tự gửi yêu cầu sang ChatGPT. Đang chờ response.json...",
                request_id=request_id,
                transport_error=None,
                owner="gpt",
                revision=revision,
                auto_gpt_status="dispatched",
                auto_gpt_detail=detail,
            )
        else:
            self._save(
                status="waiting_agent",
                stage=stage,
                message=(
                    "Không auto-dispatch được GPT. Có thể dùng nút MỞ CHATGPT / COPY YÊU CẦU "
                    "để tiếp tục thủ công."
                ),
                request_id=request_id,
                transport_error=None,
                owner="gpt",
                revision=revision,
                auto_gpt_status="manual_fallback",
                auto_gpt_detail=detail,
            )

    def _wait_for_agent(self, runner: Any, stage: str) -> None:
        config = DriveBridgeConfig.load(self.root)
        bridge = MoonDriveBridge(runner, config)
        self._command(
            f'"{os.fspath(Path(os.sys.executable))}" -m moon bridge publish "{self.root}" {stage}'
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
        batch = route.get("batch") or {}
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
                f"revision={route.get('revision')}; "
                f"batch_id={batch.get('batch_id') or '-'}"
            ),
        )

        self._dispatch_gpt(stage=stage, request=request, route=route)
        self._command(
            f'"{os.fspath(Path(os.sys.executable))}" -m moon bridge watch "{self.root}"'
        )

        response_fix_attempts = 0
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
            except BridgeResponseError as exc:
                response_fix_attempts += 1
                if response_fix_attempts >= self.auto_config.max_dispatch_attempts:
                    self._save(
                        status="waiting_agent",
                        stage=stage,
                        message=(
                            "GPT đã trả response chưa hợp lệ nhiều lần. Moon giữ nguyên request "
                            "để sửa thủ công thay vì chuyển pipeline sang Failed."
                        ),
                        request_id=request.get("request_id"),
                        transport_error=None,
                        owner="gpt",
                        revision=route.get("revision"),
                        remote_path=config.remote_path,
                        response_error=str(exc),
                        auto_gpt_status="manual_fallback",
                    )
                    self.sleep(config.poll_interval_seconds)
                    continue

                self._save(
                    status="waiting_agent",
                    stage=stage,
                    message="Response chưa hợp lệ. Local Operator đang yêu cầu GPT sửa lại...",
                    request_id=request.get("request_id"),
                    transport_error=None,
                    owner="gpt",
                    revision=route.get("revision"),
                    remote_path=config.remote_path,
                    response_error=str(exc),
                    auto_gpt_status="repairing_response",
                )
                self._dispatch_gpt(
                    stage=stage,
                    request=request,
                    route=route,
                    force=True,
                    response_error=str(exc),
                )
                self.sleep(config.poll_interval_seconds)
                continue

            if self.store.load().get("transport_error"):
                self._save(
                    status="waiting_agent",
                    stage=stage,
                    message="Đang chờ GPT hoàn thành request tự động...",
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
                    response_error=None,
                    auto_gpt_status="consumed",
                    stage_internals=(
                        f"bridge_status={consumed.get('status')}; stage={stage}; "
                        f"revision={route.get('revision')}; "
                        f"batch_id={batch.get('batch_id') or '-'}; "
                        f"remaining_batches={consumed.get('remaining_batches', '-')}"
                    ),
                )
                return
            self.sleep(config.poll_interval_seconds)

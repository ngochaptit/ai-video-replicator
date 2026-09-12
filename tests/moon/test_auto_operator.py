from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from moon.auto_operator import AutoGPTConfig, LocalGPTDesktopOperator


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".moon").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".moon" / "bridge.json").write_text(
        json.dumps({"project_id": "9.9"}), encoding="utf-8"
    )
    return tmp_path


def test_auto_gpt_config_loads_project_values(tmp_path: Path, monkeypatch) -> None:
    root = _project(tmp_path)
    (root / ".moon" / "operator.json").write_text(
        json.dumps(
            {
                "auto_chatgpt": {
                    "enabled": True,
                    "executable": "custom-interpreter",
                    "profile": "desktop-local",
                    "timeout_seconds": 240,
                    "max_dispatch_attempts": 3,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("MOON_OPERATOR_AUTO_CHATGPT", raising=False)
    monkeypatch.delenv("MOON_OPERATOR_INTERPRETER", raising=False)
    monkeypatch.delenv("MOON_OPERATOR_INTERPRETER_PROFILE", raising=False)

    config = AutoGPTConfig.load(root)

    assert config.enabled is True
    assert config.executable == "custom-interpreter"
    assert config.profile == "desktop-local"
    assert config.timeout_seconds == 240
    assert config.max_dispatch_attempts == 3


def test_dispatch_invokes_interpreter_once_and_records_request(
    tmp_path: Path, monkeypatch
) -> None:
    root = _project(tmp_path)
    config = AutoGPTConfig(
        enabled=True,
        executable="interpreter",
        profile="desktop-local",
        timeout_seconds=60,
        max_dispatch_attempts=2,
    )
    operator = LocalGPTDesktopOperator(root, config)
    monkeypatch.setattr("moon.auto_operator.shutil.which", lambda value: "C:/bin/interpreter.exe")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="submitted", stderr="")

    monkeypatch.setattr("moon.auto_operator.subprocess.run", fake_run)

    ok, detail = operator.dispatch(
        stage="footage",
        request_id="req-1",
        revision=4,
    )
    again_ok, again_detail = operator.dispatch(
        stage="footage",
        request_id="req-1",
        revision=4,
    )

    assert ok is True
    assert "Local" in detail or "local" in detail
    assert again_ok is True
    assert "trước đó" in again_detail
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[:2] == ["interpreter", "exec"]
    assert "--profile" in command
    assert command[-1] == "-"
    prompt = kwargs["input"]
    assert "LOCAL OPERATOR" in prompt
    assert "AI Video Replicator project 9.9" in prompt
    assert "stage footage" in prompt
    assert "gửi đúng MỘT lần" in prompt
    record = json.loads((root / ".moon" / "auto-gpt-dispatch.json").read_text(encoding="utf-8"))
    assert record["request_id"] == "req-1"
    assert record["revision"] == 4
    assert record["attempts"] == 1
    assert record["success"] is True


def test_dispatch_failure_keeps_manual_fallback_available(
    tmp_path: Path, monkeypatch
) -> None:
    root = _project(tmp_path)
    config = AutoGPTConfig(enabled=True, executable="interpreter")
    operator = LocalGPTDesktopOperator(root, config)
    monkeypatch.setattr("moon.auto_operator.shutil.which", lambda value: None)

    ok, detail = operator.dispatch(
        stage="footage",
        request_id="req-2",
        revision=1,
    )

    assert ok is False
    assert "chưa được cài" in detail
    assert not (root / ".moon" / "auto-gpt-dispatch.json").exists()


def test_force_dispatch_can_send_contract_repair_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    root = _project(tmp_path)
    config = AutoGPTConfig(
        enabled=True,
        executable="interpreter",
        timeout_seconds=60,
        max_dispatch_attempts=2,
    )
    operator = LocalGPTDesktopOperator(root, config)
    monkeypatch.setattr("moon.auto_operator.shutil.which", lambda value: "interpreter")
    prompts = []

    def fake_run(command, **kwargs):
        prompts.append(kwargs["input"])
        return SimpleNamespace(returncode=0, stdout="submitted", stderr="")

    monkeypatch.setattr("moon.auto_operator.subprocess.run", fake_run)

    first, _ = operator.dispatch(stage="footage", request_id="req-3", revision=1)
    repaired, _ = operator.dispatch(
        stage="footage",
        request_id="req-3",
        revision=1,
        force=True,
        response_error="batch_id does not match active batch",
    )

    assert first is True
    assert repaired is True
    assert len(prompts) == 2
    assert "batch_id does not match active batch" in prompts[1]
    assert "không phân tích lại evidence" in prompts[1]

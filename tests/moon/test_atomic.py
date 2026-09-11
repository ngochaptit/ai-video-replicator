from __future__ import annotations

import json
from pathlib import Path

import pytest

import moon.atomic as atomic_module
from moon.atomic import atomic_write_json


def test_atomic_json_retries_transient_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    real_replace = atomic_module.os.replace
    attempts = []

    def flaky_replace(source, destination):
        attempts.append((source, destination))
        if len(attempts) == 1:
            raise PermissionError(5, "temporarily locked")
        return real_replace(source, destination)

    monkeypatch.setattr(atomic_module.os, "replace", flaky_replace)
    atomic_write_json(target, {"status": "complete"}, sleep=lambda _: None)

    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "complete"}
    assert len(attempts) == 2
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_atomic_json_replace_failure_preserves_previous_valid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"status":"previous"}\n', encoding="utf-8")

    def locked_replace(_source, _destination):
        raise PermissionError(5, "still locked")

    monkeypatch.setattr(atomic_module.os, "replace", locked_replace)
    with pytest.raises(PermissionError):
        atomic_write_json(target, {"status": "new"}, retries=1, sleep=lambda _: None)

    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "previous"}
    assert list(tmp_path.glob(".state.json.*.tmp")) == []

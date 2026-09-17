from __future__ import annotations

import json
from pathlib import Path

import pytest

from moon.core.project import MoonProject
from moon.mirror.models import PROJECT_PROTOCOL
from moon.mirror.transport import MirrorTransport
from moon.tasks.exchange import TaskExchange
from moon.tasks.schemas import RESPONSE_PROTOCOL, TaskValidationError, parse_response, validate_response
from moon.tasks.store import TaskStore


def manifest() -> dict:
    return {
        "protocol": PROJECT_PROTOCOL,
        "project_id": "p",
        "generation": 3,
        "assets": [{"asset_id": "asset_a", "content_sha256": "a" * 64, "duration_seconds": 5.0}],
    }


def task() -> dict:
    return {
        "task_id": "footage_001_test",
        "revision": 1,
        "project_generation": 3,
        "result_schema": {"type": "object"},
    }


def response(**updates: object) -> dict:
    value = {
        "protocol": RESPONSE_PROTOCOL,
        "task_id": "footage_001_test",
        "revision": 1,
        "project_generation": 3,
        "status": "completed",
        "created_at": "2026-01-01T00:00:00Z",
        "result": {},
    }
    value.update(updates)
    return value


def test_parser_rejects_duplicate_keys() -> None:
    with pytest.raises(TaskValidationError, match="duplicate_key"):
        parse_response(b'{"task_id":"a","task_id":"b"}')


def test_parser_rejects_nonfinite_number() -> None:
    with pytest.raises(TaskValidationError, match="non_finite_number"):
        parse_response(b'{"value":NaN}')


def test_validation_rejects_stale_generation() -> None:
    with pytest.raises(TaskValidationError, match="stale_response"):
        validate_response(task(), response(project_generation=2), manifest())


def test_validation_binds_asset_hash_and_duration() -> None:
    bad = response(result={"asset_id": "asset_a", "content_sha256": "b" * 64, "timestamp_seconds": 6.0})
    with pytest.raises(TaskValidationError, match="stale_asset"):
        validate_response(task(), bad, manifest())


def test_needs_refinement_must_not_be_empty() -> None:
    with pytest.raises(TaskValidationError, match="empty_refinement"):
        validate_response(task(), response(status="needs_refinement", result={"requests": []}), manifest())


def test_exchange_is_idempotent_and_receipt_is_immutable(tmp_path: Path) -> None:
    project = MoonProject.open(tmp_path / "project", create=True)
    transport = MirrorTransport(tmp_path / "drive", "p")
    exchange = TaskExchange(TaskStore(project), transport)
    created = exchange.create(stage="footage", revision=1, manifest=manifest(), result_schema={"type": "object"}, inputs={"x": 1})
    assert exchange.create(stage="footage", revision=1, manifest=manifest(), result_schema={"type": "object"}, inputs={"x": 1})["task_id"] == created["task_id"]
    valid = response(task_id=created["task_id"], result={})
    transport.write_json(f"tasks/{created['task_id']}/response.json", valid)
    accepted = exchange.poll(created, manifest())
    assert accepted["status"] == "accepted"
    assert exchange.poll(created, manifest())["receipt"] == accepted["receipt"]


def test_invalid_response_writes_correction_not_receipt(tmp_path: Path) -> None:
    project = MoonProject.open(tmp_path / "project", create=True)
    transport = MirrorTransport(tmp_path / "drive", "p")
    store = TaskStore(project)
    exchange = TaskExchange(store, transport)
    created = exchange.create(stage="footage", revision=1, manifest=manifest(), result_schema={"type": "object"}, inputs={})
    transport.write_json(f"tasks/{created['task_id']}/response.json", response(task_id="wrong"))
    result = exchange.poll(created, manifest())
    assert result["status"] == "correction_required"
    assert store.read(created["task_id"], "receipt.json") is None
    assert transport.read_json(f"tasks/{created['task_id']}/correction.json")["artifact"] == "task_correction"

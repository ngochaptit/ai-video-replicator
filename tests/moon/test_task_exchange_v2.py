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
    transport.write_json(f"agent/tasks/{created['task_id']}/response.json", valid)
    accepted = exchange.poll(created, manifest())
    assert accepted["status"] == "accepted"
    assert exchange.poll(created, manifest())["receipt"] == accepted["receipt"]


def test_invalid_response_writes_correction_not_receipt(tmp_path: Path) -> None:
    project = MoonProject.open(tmp_path / "project", create=True)
    transport = MirrorTransport(tmp_path / "drive", "p")
    store = TaskStore(project)
    exchange = TaskExchange(store, transport)
    created = exchange.create(stage="footage", revision=1, manifest=manifest(), result_schema={"type": "object"}, inputs={})
    transport.write_json(f"agent/tasks/{created['task_id']}/response.json", response(task_id="wrong"))
    result = exchange.poll(created, manifest())
    assert result["status"] == "correction_required"
    assert store.read(created["task_id"], "receipt.json") is None
    assert transport.read_json(f"agent/tasks/{created['task_id']}/validation.json")["artifact"] == "task_correction"


def test_correction_retry_accepts_replaced_response(tmp_path: Path) -> None:
    project = MoonProject.open(tmp_path / "project", create=True)
    transport = MirrorTransport(tmp_path / "drive", "p")
    exchange = TaskExchange(TaskStore(project), transport)
    created = exchange.create(stage="footage", revision=1, manifest=manifest(), result_schema={"type": "object"}, inputs={})
    remote = f"agent/tasks/{created['task_id']}/response.json"
    transport.write_json(remote, response(task_id="wrong"))
    assert exchange.poll(created, manifest())["status"] == "correction_required"
    transport.write_json(remote, response(task_id=created["task_id"]))
    assert exchange.poll(created, manifest())["status"] == "accepted"
    assert transport.read_json("agent/current.json")["status"] == "ACCEPTED"


def test_semantic_validation_happens_before_receipt(tmp_path: Path) -> None:
    project = MoonProject.open(tmp_path / "project", create=True)
    transport = MirrorTransport(tmp_path / "drive", "p")
    store = TaskStore(project)
    exchange = TaskExchange(store, transport)
    created = exchange.create(stage="footage", revision=1, manifest=manifest(), result_schema={"type": "object"}, inputs={})
    transport.write_json(f"agent/tasks/{created['task_id']}/response.json", response(task_id=created["task_id"]))

    result = exchange.poll(
        created,
        manifest(),
        semantic_validator=lambda value: (_ for _ in ()).throw(ValueError("bad semantics")),
    )

    assert result["status"] == "correction_required"
    assert store.read(created["task_id"], "receipt.json") is None


def test_apply_recovers_after_interrupted_attempt(tmp_path: Path) -> None:
    project = MoonProject.open(tmp_path / "project", create=True)
    transport = MirrorTransport(tmp_path / "drive", "p")
    exchange = TaskExchange(TaskStore(project), transport)
    created = exchange.create(stage="footage", revision=1, manifest=manifest(), result_schema={"type": "object"}, inputs={})
    transport.write_json(f"agent/tasks/{created['task_id']}/response.json", response(task_id=created["task_id"]))
    exchange.poll(created, manifest())
    attempts = 0

    def callback(value: dict) -> dict:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated crash")
        return {"recovered": True}

    with pytest.raises(RuntimeError, match="simulated crash"):
        exchange.apply(created, callback)
    applied = exchange.apply(created, callback)

    assert applied["attempt"] == 2
    assert applied["result"] == {"recovered": True}
    assert transport.read_json("agent/current.json")["status"] == "CONSUMED"


def test_refinement_child_preserves_partial_and_sampled_evidence(tmp_path: Path) -> None:
    project = MoonProject.open(tmp_path / "project", create=True)
    transport = MirrorTransport(tmp_path / "drive", "p")
    exchange = TaskExchange(TaskStore(project), transport)
    parent = exchange.create(stage="footage", revision=1, manifest=manifest(), result_schema={"type": "object"}, inputs={})
    child = exchange.create_refinement(
        parent,
        manifest(),
        [{"clip_id": "c1", "start_seconds": 0.0, "end_seconds": 1.0, "reason": "boundary"}],
        {"type": "object"},
        partial_result={"clips": [{"clip_id": "done"}]},
        sampled_evidence={"groups": [{"group_id": "g1"}]},
    )

    assert child["parent_task_id"] == parent["task_id"]
    assert child["inputs"]["partial_result"]["clips"][0]["clip_id"] == "done"
    assert child["inputs"]["sampled_evidence"]["groups"][0]["group_id"] == "g1"

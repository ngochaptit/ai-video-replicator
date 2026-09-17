from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from moon.mirror.models import PROJECT_PROTOCOL
from moon.mirror.transport import MirrorTransport
from moon.tasks.schemas import TASK_PROTOCOL, TaskValidationError, parse_response, validate_response
from moon.tasks.store import TaskStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class TaskExchange:
    def __init__(self, store: TaskStore, transport: MirrorTransport) -> None:
        self.store = store
        self.transport = transport

    def create(
        self,
        *,
        stage: str,
        revision: int,
        manifest: dict[str, Any],
        result_schema: dict[str, Any],
        inputs: dict[str, Any],
        parent_task_id: str | None = None,
        task_kind: str = "analysis",
    ) -> dict[str, Any]:
        if manifest.get("protocol") != PROJECT_PROTOCOL:
            raise ValueError("tasks require a project mirror V2 manifest")
        identity = {
            "stage": stage,
            "revision": revision,
            "project_generation": manifest["generation"],
            "result_schema": result_schema,
            "inputs": inputs,
            "parent_task_id": parent_task_id,
            "task_kind": task_kind,
        }
        signature = _digest(identity)
        existing = self.store.find_signature(signature)
        if existing:
            return existing
        task_id = f"{stage}_{revision:03d}_{signature[:12]}"
        task = {
            "protocol": TASK_PROTOCOL,
            "task_id": task_id,
            "signature": signature,
            "stage": stage,
            "task_kind": task_kind,
            "revision": revision,
            "project_generation": manifest["generation"],
            "parent_task_id": parent_task_id,
            "created_at": _now(),
            "manifest_path": "manifest.json",
            "inputs": inputs,
            "result_schema": result_schema,
            "response_path": f"tasks/{task_id}/response.json",
        }
        self.store.write_once(task_id, "request.json", task)
        self.store.record(task)
        self.transport.write_json(f"tasks/{task_id}/request.json", task)
        self.transport.write_json("tasks/current.json", {"protocol": TASK_PROTOCOL, "task_id": task_id, "request_path": f"tasks/{task_id}/request.json"})
        return task

    def poll(self, task: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
        task_id = str(task["task_id"])
        raw = self.transport.read_bytes(f"tasks/{task_id}/response.json")
        if raw is None:
            return {"status": "waiting", "task_id": task_id}
        existing_receipt = self.store.read(task_id, "receipt.json")
        if existing_receipt:
            response_sha256 = hashlib.sha256(raw).hexdigest()
            if response_sha256 != existing_receipt.get("response_sha256"):
                raise ValueError("response changed after immutable receipt was recorded")
            return {"status": "accepted", "task_id": task_id, "receipt": existing_receipt}
        try:
            response = parse_response(raw)
            validate_response(task, response, manifest)
        except TaskValidationError as exc:
            correction = exc.correction(task)
            self.store.write_state(task_id, "validation.json", correction)
            self.transport.write_json(f"tasks/{task_id}/correction.json", correction)
            return {"status": "correction_required", "task_id": task_id, "error": correction["error"]}
        response_sha256 = hashlib.sha256(raw).hexdigest()
        self.store.write_once(task_id, "response.json", response)
        validation = {"valid": True, "validated_at": _now(), "response_sha256": response_sha256}
        self.store.write_state(task_id, "validation.json", validation)
        if response.get("status") in {"partial", "needs_refinement"}:
            self.store.write_once(task_id, "partial.json", response)
        receipt = {
            "protocol": TASK_PROTOCOL,
            "task_id": task_id,
            "revision": task["revision"],
            "project_generation": task["project_generation"],
            "response_sha256": response_sha256,
            "accepted_at": _now(),
            "response_status": response["status"],
        }
        self.store.write_once(task_id, "receipt.json", receipt)
        self.transport.write_json(f"tasks/{task_id}/receipt.json", receipt)
        return {"status": "accepted", "task_id": task_id, "response": response, "receipt": receipt}

    def apply(self, task: dict[str, Any], callback: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        task_id = str(task["task_id"])
        applied = self.store.read(task_id, "APPLIED.json")
        if applied:
            return applied
        response = self.store.read(task_id, "response.json")
        receipt = self.store.read(task_id, "receipt.json")
        if response is None or receipt is None:
            raise ValueError("task response has not been accepted")
        applying = {"task_id": task_id, "started_at": _now(), "response_sha256": receipt["response_sha256"]}
        self.store.write_state(task_id, "APPLYING.json", applying)
        result = callback(response)
        marker = {**applying, "completed_at": _now(), "result": result}
        self.store.write_once(task_id, "APPLIED.json", marker)
        self.transport.write_json(f"tasks/{task_id}/APPLIED.json", marker)
        return marker

    def create_refinement(
        self,
        parent: dict[str, Any],
        manifest: dict[str, Any],
        requests: list[dict[str, Any]],
        result_schema: dict[str, Any],
    ) -> dict[str, Any]:
        if not requests:
            raise ValueError("refinement requests must not be empty")
        return self.create(
            stage=str(parent["stage"]),
            revision=int(parent["revision"]) + 1,
            manifest=manifest,
            result_schema=result_schema,
            inputs={"refinement_requests": requests, "partial_task_id": parent["task_id"]},
            parent_task_id=str(parent["task_id"]),
            task_kind="refinement",
        )

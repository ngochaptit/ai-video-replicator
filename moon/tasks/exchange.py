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
            "project_id": self.transport.root.name,
            "pipeline_revision": revision,
            "manifest_path": "project_manifest.json",
            "inputs": inputs,
            "scope": {"assets": inputs.get("asset_scope") or []},
            "expected_output": stage,
            "result_schema": result_schema,
            "response_path": f"agent/tasks/{task_id}/response.json",
        }
        self.store.write_once(task_id, "request.json", task)
        self.store.record(task)
        request_path = f"agent/tasks/{task_id}/request.json"
        self.transport.write_json(request_path, task)
        self.transport.write_json(
            "agent/current.json",
            {
                "protocol": TASK_PROTOCOL,
                "project_id": self.transport.root.name,
                "active_task": task_id,
                "task_id": task_id,
                "stage": stage,
                "status": "WAITING_GPT",
                "request_path": request_path,
            },
        )
        return task

    def _project_current(
        self, task: dict[str, Any], status: str, **extra: Any
    ) -> None:
        self.transport.write_json(
            "agent/current.json",
            {
                "protocol": TASK_PROTOCOL,
                "project_id": self.transport.root.name,
                "active_task": task["task_id"],
                "task_id": task["task_id"],
                "stage": task["stage"],
                "status": status,
                "request_path": f"agent/tasks/{task['task_id']}/request.json",
                **extra,
            },
        )

    def poll(
        self,
        task: dict[str, Any],
        manifest: dict[str, Any],
        *,
        semantic_validator: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        task_id = str(task["task_id"])
        remote_dir = f"agent/tasks/{task_id}"
        raw = self.transport.read_bytes(f"{remote_dir}/response.json")
        if raw is None:
            return {"status": "waiting", "task_id": task_id}
        existing_receipt = self.store.read(task_id, "receipt.json")
        if existing_receipt:
            response_sha256 = hashlib.sha256(raw).hexdigest()
            return {
                "status": "accepted",
                "task_id": task_id,
                "receipt": existing_receipt,
                "immutable_response_changed": response_sha256 != existing_receipt.get("response_sha256"),
            }
        try:
            response = parse_response(raw)
            validate_response(task, response, manifest)
            if semantic_validator is not None:
                semantic_validator(response)
        except (TaskValidationError, ValueError, TypeError, KeyError) as exc:
            if not isinstance(exc, TaskValidationError):
                exc = TaskValidationError("invalid_semantic_result", str(exc), "result")
            correction = exc.correction(task)
            self.store.write_state(task_id, "validation.json", correction)
            self.transport.write_json(f"{remote_dir}/validation.json", correction)
            self._project_current(
                task,
                "WAITING_GPT_CORRECTION",
                validation_path=f"{remote_dir}/validation.json",
            )
            return {"status": "correction_required", "task_id": task_id, "error": correction["error"]}
        response_sha256 = hashlib.sha256(raw).hexdigest()
        self.store.write_once(task_id, "response.json", response)
        validation = {"valid": True, "validated_at": _now(), "response_sha256": response_sha256}
        self.store.write_state(task_id, "validation.json", validation)
        self.transport.write_json(f"{remote_dir}/validation.json", validation)
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
        self.transport.write_json(f"{remote_dir}/receipt.json", receipt)
        self._project_current(
            task,
            "ACCEPTED",
            receipt_path=f"{remote_dir}/receipt.json",
        )
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
        prior_applying = self.store.read(task_id, "APPLYING.json")
        if prior_applying and prior_applying.get("response_sha256") != receipt["response_sha256"]:
            raise ValueError("APPLYING marker does not match accepted response")
        applying = prior_applying or {
            "task_id": task_id,
            "started_at": _now(),
            "response_sha256": receipt["response_sha256"],
            "attempt": 0,
        }
        applying["attempt"] = int(applying.get("attempt", 0)) + 1
        applying["last_attempt_at"] = _now()
        self.store.write_state(task_id, "APPLYING.json", applying)
        try:
            result = callback(response)
        except Exception as exc:
            applying["last_error"] = str(exc)
            self.store.write_state(task_id, "APPLYING.json", applying)
            raise
        marker = {**applying, "completed_at": _now(), "result": result}
        self.store.write_once(task_id, "APPLIED.json", marker)
        self.transport.write_json(f"agent/tasks/{task_id}/APPLIED.json", marker)
        self._project_current(
            task,
            "CONSUMED",
            receipt_path=f"agent/tasks/{task_id}/receipt.json",
            applied_path=f"agent/tasks/{task_id}/APPLIED.json",
        )
        return marker

    def create_refinement(
        self,
        parent: dict[str, Any],
        manifest: dict[str, Any],
        requests: list[dict[str, Any]],
        result_schema: dict[str, Any],
        *,
        partial_result: dict[str, Any] | None = None,
        sampled_evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not requests:
            raise ValueError("refinement requests must not be empty")
        return self.create(
            stage=str(parent["stage"]),
            revision=int(parent["revision"]) + 1,
            manifest=manifest,
            result_schema=result_schema,
            inputs={
                "refinement_requests": requests,
                "partial_task_id": parent["task_id"],
                "partial_result": partial_result or {},
                "sampled_evidence": sampled_evidence or {},
            },
            parent_task_id=str(parent["task_id"]),
            task_kind="refinement",
        )

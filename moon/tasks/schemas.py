from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from jsonschema import Draft202012Validator

from moon.mirror.models import PROJECT_PROTOCOL


TASK_PROTOCOL = "mon_edit_task/2"
RESPONSE_PROTOCOL = "mon_edit_response/2"
MAX_RESPONSE_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class TaskValidationError(ValueError):
    code: str
    message: str
    path: str = "<root>"

    def __str__(self) -> str:
        return f"{self.code}: {self.path}: {self.message}"

    def correction(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocol": TASK_PROTOCOL,
            "artifact": "task_correction",
            "task_id": task.get("task_id"),
            "revision": task.get("revision"),
            "error": {"code": self.code, "path": self.path, "message": self.message},
            "instruction": "Replace response.json with a corrected response for this exact task identity.",
        }


def parse_response(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise TaskValidationError("response_too_large", f"response exceeds {MAX_RESPONSE_BYTES} bytes")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise TaskValidationError("duplicate_key", f"duplicate JSON key {key!r}", key)
            result[key] = value
        return result

    def nonfinite(value: str) -> Any:
        raise TaskValidationError("non_finite_number", f"non-finite JSON number {value!r}")

    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=nonfinite)
    except UnicodeDecodeError as exc:
        raise TaskValidationError("invalid_utf8", "response must be UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise TaskValidationError("invalid_json", exc.msg, f"line {exc.lineno} column {exc.colno}") from exc
    if not isinstance(payload, dict):
        raise TaskValidationError("invalid_envelope", "response must be one JSON object")
    return payload


def validate_response(
    task: dict[str, Any],
    response: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    required = {"protocol", "task_id", "revision", "project_generation", "status", "created_at", "result"}
    missing = sorted(required - response.keys())
    if missing:
        raise TaskValidationError("missing_field", f"missing required field(s): {', '.join(missing)}")
    if response.get("protocol") != RESPONSE_PROTOCOL:
        raise TaskValidationError("wrong_protocol", f"expected {RESPONSE_PROTOCOL!r}", "protocol")
    for field in ("task_id", "revision", "project_generation"):
        if response.get(field) != task.get(field):
            code = "stale_response" if field in {"revision", "project_generation"} else "wrong_task"
            raise TaskValidationError(code, f"expected {task.get(field)!r}, got {response.get(field)!r}", field)
    if response.get("status") not in {"completed", "partial", "needs_refinement", "rejected"}:
        raise TaskValidationError("invalid_status", "unsupported response status", "status")
    _parse_timestamp(response.get("created_at"), "created_at")
    result = response.get("result")
    schema = task.get("result_schema") or {"type": "object"}
    errors = sorted(Draft202012Validator(schema).iter_errors(result), key=lambda error: list(error.path))
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path) or "result"
        raise TaskValidationError("invalid_result", error.message, path)
    if manifest.get("protocol") != PROJECT_PROTOCOL:
        raise TaskValidationError("invalid_manifest", "task must validate against a project mirror V2 manifest")
    assets = {item.get("asset_id"): item for item in manifest.get("assets", [])}
    _validate_asset_references(result, assets)
    if response.get("status") == "needs_refinement":
        requests = result.get("requests") if isinstance(result, dict) else None
        if not isinstance(requests, list) or not requests:
            raise TaskValidationError("empty_refinement", "needs_refinement requires a non-empty requests array", "result.requests")
    return response


def _validate_asset_references(value: Any, assets: dict[str, dict[str, Any]], path: str = "result") -> None:
    if isinstance(value, dict):
        asset_id = value.get("asset_id")
        if asset_id is not None:
            if asset_id not in assets:
                raise TaskValidationError("unknown_asset", f"unknown asset_id {asset_id!r}", f"{path}.asset_id")
            expected = assets[asset_id].get("content_sha256")
            supplied = value.get("content_sha256")
            if supplied is not None and supplied != expected:
                raise TaskValidationError("stale_asset", f"content hash does not match {asset_id}", f"{path}.content_sha256")
            duration = assets[asset_id].get("duration_seconds")
            if duration is not None:
                for key in ("timestamp", "timestamp_seconds", "start_seconds", "end_seconds", "source_in", "source_out"):
                    if key in value:
                        number = value[key]
                        if not isinstance(number, (int, float)) or isinstance(number, bool) or not math.isfinite(number):
                            raise TaskValidationError("invalid_timestamp", "timestamp must be finite", f"{path}.{key}")
                        if number < 0 or number > float(duration) + 1e-6:
                            raise TaskValidationError("timestamp_out_of_range", f"timestamp exceeds asset duration {duration}", f"{path}.{key}")
                if "start_seconds" in value and "end_seconds" in value and value["end_seconds"] <= value["start_seconds"]:
                    raise TaskValidationError("invalid_range", "end_seconds must be greater than start_seconds", path)
        for key, child in value.items():
            _validate_asset_references(child, assets, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_asset_references(child, assets, f"{path}[{index}]")


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise TaskValidationError("invalid_timestamp", "must be an ISO-8601 timestamp", field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TaskValidationError("invalid_timestamp", "must be an ISO-8601 timestamp", field) from exc
    if parsed.tzinfo is None:
        raise TaskValidationError("invalid_timestamp", "timezone is required", field)
    return parsed.astimezone(timezone.utc)

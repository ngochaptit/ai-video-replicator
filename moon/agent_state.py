from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from moon.atomic import atomic_write_json


AGENT_STATE_VERSION = "1.0"
REQUIRED_AGENT_STATE_FIELDS = {
    "version",
    "job_id",
    "stage",
    "revision",
    "status",
    "current_actor",
    "next_actor",
    "next_action",
    "task",
    "required_inputs",
    "expected_output",
    "completion_contract",
    "updated_at",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class AgentStateStore:
    """Moon-owned routing cache reconstructed from pipeline and bridge artifacts."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not REQUIRED_AGENT_STATE_FIELDS <= set(payload):
            return None
        if payload.get("version") != AGENT_STATE_VERSION:
            return None
        return payload

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = dict(payload)
        state["version"] = AGENT_STATE_VERSION
        state.setdefault("updated_at", utc_now())
        missing = sorted(REQUIRED_AGENT_STATE_FIELDS - set(state))
        if missing:
            raise ValueError(f"agent state is missing required fields: {', '.join(missing)}")
        atomic_write_json(self.path, state)
        return state


def transition(
    state: dict[str, Any],
    status: str,
    *,
    current_actor: str,
    next_actor: str,
    next_action: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    updated = dict(state)
    updated.update(
        status=status,
        current_actor=current_actor,
        next_actor=next_actor,
        next_action=next_action,
        updated_at=utc_now(),
    )
    if metadata:
        updated.update(metadata)
    history = list(updated.get("transition_history") or [])
    history.append(
        {
            "status": status,
            "current_actor": current_actor,
            "next_actor": next_actor,
            "next_action": next_action,
            "at": updated["updated_at"],
        }
    )
    updated["transition_history"] = history[-50:]
    return updated

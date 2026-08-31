from __future__ import annotations

import json
from typing import Any

from moon.execution import StageExecutionService
from moon.handoff import AgentHandoffService
from moon.runner.pipeline import PipelineRunner


class AgentBridgeService:
    """Agent-neutral bridge for progressing Moon and exchanging semantic decisions."""

    def __init__(self, runner: PipelineRunner) -> None:
        self.runner = runner

    def next(self, *, max_steps: int = 20) -> dict[str, Any]:
        """Run deterministic stages until completion, blockage, or an agent boundary."""
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")

        execution = StageExecutionService(self.runner)
        history: list[dict[str, Any]] = []

        for _ in range(max_steps):
            stage = self.runner.state.next_stage()
            if stage is None:
                return {
                    "status": "complete",
                    "history": history,
                    "pipeline": self.runner.status(),
                }

            result = execution.run()
            history.append({
                "stage": stage,
                "status": result.get("status"),
            })

            status = result.get("status")
            if status == "completed":
                continue
            if status == "awaiting_agent":
                handoff = AgentHandoffService(self.runner).package(stage)
                return {
                    "status": "awaiting_agent",
                    "stage": stage,
                    "handoff": handoff,
                    "history": history,
                    "pipeline": self.runner.status(),
                }
            if status == "blocked":
                return {
                    "status": "blocked",
                    "stage": stage,
                    "result": result,
                    "history": history,
                    "pipeline": self.runner.status(),
                }
            if status == "complete":
                return {
                    "status": "complete",
                    "history": history,
                    "pipeline": self.runner.status(),
                }
            raise RuntimeError(f"unexpected stage status: {status!r}")

        return {
            "status": "step_limit",
            "history": history,
            "pipeline": self.runner.status(),
        }

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Handle one stdin/stdout bridge request without temporary files."""
        action = payload.get("action")
        if action == "next":
            max_steps = payload.get("max_steps", 20)
            if isinstance(max_steps, bool) or not isinstance(max_steps, int):
                raise ValueError("next.max_steps must be an integer")
            return self.next(max_steps=max_steps)
        if action == "handoff":
            stage = payload.get("stage")
            if stage is not None and not isinstance(stage, str):
                raise ValueError("handoff.stage must be a string when provided")
            return AgentHandoffService(self.runner).package(stage)
        if action == "submit":
            stage = payload.get("stage")
            response = payload.get("payload")
            if not isinstance(stage, str) or not isinstance(response, dict):
                raise ValueError("submit requires string stage and object payload")
            accepted = AgentHandoffService(self.runner).submit(stage, response)
            auto_next = payload.get("auto_next", True)
            if not isinstance(auto_next, bool):
                raise ValueError("submit.auto_next must be boolean")
            if not auto_next:
                return {"status": "accepted", "submission": accepted}
            return {
                "status": "accepted_and_advanced",
                "submission": accepted,
                "next": self.next(),
            }
        raise ValueError(f"unknown agent bridge action: {action!r}")


def parse_bridge_request(raw: str) -> dict[str, Any]:
    if not raw.strip():
        raise ValueError("agent bridge requires one JSON object on stdin")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("agent bridge stdin must be a JSON object")
    return payload

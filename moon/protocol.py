from __future__ import annotations

from typing import Any

from moon.runner.pipeline import PipelineRunner


class MoonProtocol:
    """Small agent-neutral JSON command surface for Moon Local."""

    def __init__(self, runner: PipelineRunner) -> None:
        self.runner = runner

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        if action == "status":
            return {"ok": True, "result": self.runner.status()}
        if action == "resume":
            return {"ok": True, "result": self.runner.resume()}
        if action == "begin":
            stage = request.get("stage")
            return {"ok": True, "result": {"stage": self.runner.begin(stage)}}
        if action == "complete":
            stage = request.get("stage")
            checkpoint = request.get("checkpoint")
            if not isinstance(stage, str):
                raise ValueError("complete requires string field 'stage'")
            if not isinstance(checkpoint, dict):
                raise ValueError("complete requires object field 'checkpoint'")
            return {"ok": True, "result": self.runner.complete(stage, checkpoint)}
        if action == "artifact.write":
            name = request.get("name")
            payload = request.get("payload")
            if not isinstance(name, str) or not isinstance(payload, dict):
                raise ValueError("artifact.write requires 'name' and object 'payload'")
            path = self.runner.artifacts.write(name, payload)
            return {"ok": True, "result": {"path": str(path)}}
        if action == "artifact.read":
            name = request.get("name")
            if not isinstance(name, str):
                raise ValueError("artifact.read requires string field 'name'")
            return {"ok": True, "result": self.runner.artifacts.read(name)}
        raise ValueError(f"unknown Moon action: {action!r}")

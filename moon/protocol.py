from __future__ import annotations

from pathlib import Path
from typing import Any

from moon.bridge import (
    bootstrap_legacy_state,
    complete_from_imported_artifacts,
    discover_existing_artifacts,
    import_existing_artifacts,
)
from moon.execution import StageExecutionService
from moon.media.frames import sample_frames
from moon.media.inspection import inspect_footage, inspect_reference, resolve_project_source
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
        if action == "stage.plan":
            return {"ok": True, "result": StageExecutionService(self.runner).plan()}
        if action == "stage.run":
            return {"ok": True, "result": StageExecutionService(self.runner).run()}
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
        if action == "artifact.discover":
            return {"ok": True, "result": discover_existing_artifacts(self.runner.project.root)}
        if action == "artifact.import":
            stage = request.get("stage")
            if stage is not None and not isinstance(stage, str):
                raise ValueError("artifact.import field 'stage' must be a string when provided")
            return {"ok": True, "result": import_existing_artifacts(self.runner, stage=stage).as_dict()}
        if action == "stage.complete_from_artifacts":
            stage = request.get("stage")
            if stage is not None and not isinstance(stage, str):
                raise ValueError("stage.complete_from_artifacts field 'stage' must be a string when provided")
            return {"ok": True, "result": complete_from_imported_artifacts(self.runner, stage=stage)}
        if action == "stage.bootstrap_legacy":
            return {"ok": True, "result": bootstrap_legacy_state(self.runner).as_dict()}
        if action == "media.inspect.reference":
            return {"ok": True, "result": inspect_reference(self.runner.project)}
        if action == "media.inspect.footage":
            return {"ok": True, "result": inspect_footage(self.runner.project)}
        if action == "media.frames":
            source = request.get("source")
            if not isinstance(source, str):
                raise ValueError("media.frames requires string field 'source'")
            start = _number(request.get("start_seconds"), "start_seconds")
            end = _number(request.get("end_seconds"), "end_seconds")
            count = int(request.get("count", 8))
            width = int(request.get("width", 320))
            source_path = resolve_project_source(self.runner.project, source)
            cache_dir = self.runner.project.cache_dir / "frames" / _safe_cache_name(Path(source).stem, start, end)
            result = sample_frames(
                source_path,
                cache_dir,
                start_seconds=start,
                end_seconds=end,
                count=count,
                width=width,
            )
            return {"ok": True, "result": result}
        raise ValueError(f"unknown Moon action: {action!r}")


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"media.frames requires numeric field {field!r}")
    return float(value)


def _safe_cache_name(stem: str, start: float, end: float) -> str:
    safe_stem = "".join(character if character.isalnum() or character in "-_" else "_" for character in stem)
    return f"{safe_stem}_{start:.3f}_{end:.3f}"

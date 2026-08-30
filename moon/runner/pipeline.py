from __future__ import annotations

from typing import Any

from moon.core.artifacts import ArtifactStore
from moon.core.project import MoonProject
from moon.core.state import PipelineState
from moon.runner.checkpoint import CheckpointStore


class PipelineRunner:
    def __init__(self, project: MoonProject) -> None:
        self.project = project
        self.project.ensure_layout()
        self.state = PipelineState.load(project.state_path)
        self.checkpoints = CheckpointStore(project.checkpoints_dir)
        self.artifacts = ArtifactStore(project.artifacts_dir)
        if not project.state_path.exists():
            self.state.save(project.state_path)

    def status(self) -> dict[str, Any]:
        next_stage = self.state.next_stage()
        return {
            "status": self.state.status,
            "current_stage": self.state.current_stage or next_stage,
            "next_stage": next_stage,
            "completed": list(self.state.completed),
            "revision": self.state.revision,
            "done": next_stage is None,
        }

    def begin(self, stage: str | None = None) -> str | None:
        target = stage or self.state.next_stage()
        if target is None:
            self.state.status = "complete"
            self.state.current_stage = None
            self.state.save(self.project.state_path)
            return None
        if target not in self.state.stages:
            raise ValueError(f"unknown Moon stage: {target}")
        expected = self.state.next_stage()
        if target != expected:
            raise ValueError(f"cannot begin {target!r}; next resumable stage is {expected!r}")
        self.state.current_stage = target
        self.state.status = "running"
        self.state.save(self.project.state_path)
        return target

    def complete(self, stage: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
        if stage not in self.state.stages:
            raise ValueError(f"unknown Moon stage: {stage}")
        expected = self.state.next_stage()
        if stage != expected:
            raise ValueError(f"cannot complete {stage!r}; expected {expected!r}")
        self.checkpoints.write(stage, checkpoint)
        self.state.completed.append(stage)
        self.state.current_stage = None
        self.state.status = "complete" if self.state.next_stage() is None else "idle"
        self.state.save(self.project.state_path)
        return self.status()

    def fail(self, stage: str, reason: str) -> None:
        self.state.current_stage = stage
        self.state.status = "blocked"
        self.state.metadata["blocker"] = {"stage": stage, "reason": reason}
        self.state.save(self.project.state_path)

    def resume(self) -> dict[str, Any]:
        self.state.status = "idle" if self.state.next_stage() is not None else "complete"
        self.state.current_stage = None
        self.state.save(self.project.state_path)
        return self.status()

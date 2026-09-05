from __future__ import annotations

from typing import Any

from moon.core.artifacts import ArtifactStore
from moon.core.project import MoonProject
from moon.core.state import PipelineState
from moon.runner.checkpoint import CheckpointStore


# Agent submissions and deterministic outputs owned by each stage. Revision
# invalidation deliberately excludes durable upstream evidence and approvals:
# decision_log is append-only governance history, while render_plan records the
# user's approved runtime and is safe to reuse when the runtime is unchanged.
_REVISION_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "proposal": ("proposal_packet", "proposal_agent_task", "proposal_handoff"),
    "analyze": (
        "reference_blueprint_scaffold",
        "video_analysis_brief",
        "reference_blueprint",
        "semantic_enrichment",
        "analyze_agent_task",
        "analyze_handoff",
    ),
    "footage": (
        "footage_profiles_scaffold",
        "footage_evidence_catalog",
        "footage_agent_task",
        "footage_handoff",
        "footage_semantic_enrichment",
        "footage_profiles",
    ),
    "match": (
        "candidate_rankings",
        "match_agent_task",
        "match_handoff",
        "match_proposal",
        "match_decisions",
    ),
    "timeline": ("timeline",),
    "render": (
        "render_agent_task",
        "render_handoff",
        "replication_render_plan",
        "draft_render",
    ),
    "qc": (
        "qc_agent_task",
        "qc_handoff",
        "qc_bundle",
        "qc_report",
        "replication_quality_report",
        "replication_qc_evidence",
        "replication_qc",
        "final_render",
    ),
}


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
        return {"status": self.state.status, "current_stage": self.state.current_stage or next_stage, "next_stage": next_stage, "completed": list(self.state.completed), "revision": self.state.revision, "done": next_stage is None}

    def begin(self, stage: str | None = None) -> str | None:
        target = stage or self.state.next_stage()
        if target is None:
            self.state.status = "complete"; self.state.current_stage = None; self.state.save(self.project.state_path); return None
        if target not in self.state.stages: raise ValueError(f"unknown Moon stage: {target}")
        expected = self.state.next_stage()
        if target != expected: raise ValueError(f"cannot begin {target!r}; next resumable stage is {expected!r}")
        self.state.current_stage = target; self.state.status = "running"; self.state.save(self.project.state_path); return target

    def complete(self, stage: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
        if stage not in self.state.stages: raise ValueError(f"unknown Moon stage: {stage}")
        expected = self.state.next_stage()
        if stage != expected: raise ValueError(f"cannot complete {stage!r}; expected {expected!r}")
        self.checkpoints.write(stage, checkpoint); self.state.completed.append(stage); self.state.current_stage = None
        self.state.status = "complete" if self.state.next_stage() is None else "idle"; self.state.save(self.project.state_path); return self.status()

    def request_revision(self, *, from_stage: str = "render", reason: str = "", max_revisions: int = 2) -> dict[str, Any]:
        if from_stage not in self.state.stages: raise ValueError(f"unknown Moon revision stage: {from_stage}")
        if self.state.revision >= max_revisions: raise ValueError(f"revision limit reached ({max_revisions})")
        start = self.state.stages.index(from_stage)
        reset = set(self.state.stages[start:])
        invalidated_artifacts: list[str] = []
        invalidated_checkpoints: list[str] = []
        for stage in self.state.stages[start:]:
            for artifact in _REVISION_ARTIFACTS.get(stage, ()):
                if self.artifacts.delete(artifact):
                    invalidated_artifacts.append(artifact)
            if self.checkpoints.delete(stage):
                invalidated_checkpoints.append(stage)
        self.state.completed = [stage for stage in self.state.completed if stage not in reset]
        self.state.revision += 1
        self.state.current_stage = None; self.state.status = "idle"
        self.state.metadata.pop("blocker", None)
        self.state.metadata["last_revision"] = {
            "from_stage": from_stage,
            "reason": reason,
            "revision": self.state.revision,
            "invalidated_artifacts": invalidated_artifacts,
            "invalidated_checkpoints": invalidated_checkpoints,
        }
        self.state.save(self.project.state_path)
        return self.status()

    def fail(self, stage: str, reason: str) -> None:
        self.state.current_stage = stage; self.state.status = "blocked"; self.state.metadata["blocker"] = {"stage": stage, "reason": reason}; self.state.save(self.project.state_path)

    def resume(self) -> dict[str, Any]:
        self.state.status = "idle" if self.state.next_stage() is not None else "complete"; self.state.current_stage = None; self.state.save(self.project.state_path); return self.status()

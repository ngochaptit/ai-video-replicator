from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from moon.runner.pipeline import PipelineRunner


HANDOFF_VERSION = "1.1"


class AgentHandoffService:
    """Build portable, inspectable task packages for external semantic agents."""

    def __init__(self, runner: PipelineRunner) -> None:
        self.runner = runner

    def package(self, stage: str | None = None) -> dict[str, Any]:
        stage = stage or self.runner.state.next_stage()
        if stage is None:
            return {"done": True, "stage": None}
        if stage != self.runner.state.next_stage():
            raise ValueError(f"handoff may only package current stage {self.runner.state.next_stage()!r}")

        task_name = f"{stage}_agent_task"
        if not self.runner.artifacts.exists(task_name):
            raise FileNotFoundError(f"run-stage first; missing handoff task artifact {task_name!r}")
        task = self.runner.artifacts.read(task_name)
        project = self.runner.project.root
        package = {
            "version": HANDOFF_VERSION,
            "stage": stage,
            "decision_owner": "external_agent",
            "project_root": str(project),
            "pipeline": self.runner.status(),
            "task": task,
            "inputs": self._inputs(stage),
            "output_contract": self._output_contract(stage),
            "submission": {
                "preferred": "stdin",
                "stdin_command": f'python -m moon --project "{project}" submit-handoff-stdin {stage}',
                "file_command": f'python -m moon --project "{project}" submit-handoff {stage} <response.json>',
                "bridge_command": f'python -m moon --project "{project}" agent-bridge',
                "then": f'python -m moon --project "{project}" next',
            },
        }
        package["handoff_id"] = self._handoff_id(package)
        self.runner.artifacts.write(f"{stage}_handoff", package)
        return package

    def submit(self, stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.runner.state.next_stage()
        if stage != current:
            raise ValueError(f"handoff submission is for {stage!r}, current stage is {current!r}")
        self._validate(stage, payload)
        artifact_name = self._required_artifact(stage)
        path = self.runner.artifacts.write(artifact_name, payload)
        return {
            "accepted": True,
            "stage": stage,
            "artifact": artifact_name,
            "path": str(path),
            "next_action": "next",
        }

    def _inputs(self, stage: str) -> dict[str, Any]:
        names = {
            "footage": ["footage_profiles_scaffold"],
            "match": ["reference_blueprint", "footage_profiles", "candidate_rankings"],
            "render": ["timeline"],
            "qc": ["draft_render"],
        }.get(stage, [])
        result: dict[str, Any] = {}
        for name in names:
            if self.runner.artifacts.exists(name):
                path = self.runner.artifacts.path_for(name)
                result[name] = {"path": str(path), "sha256": self._sha256(path)}
        task = self.runner.artifacts.read(f"{stage}_agent_task")
        evidence_root = task.get("evidence_root")
        if evidence_root:
            root = Path(evidence_root)
            files = []
            if root.is_dir():
                files = [str(path) for path in sorted(root.rglob("*")) if path.is_file()][:500]
            result["evidence"] = {"root": str(root), "files": files}
        return result

    @staticmethod
    def _output_contract(stage: str) -> dict[str, Any]:
        contracts: dict[str, dict[str, Any]] = {
            "footage": {
                "artifact": "footage_semantic_enrichment",
                "type": "object",
                "required": ["clips"],
                "rules": [
                    "clips must be a non-empty array",
                    "each clip requires a non-empty path and segments array",
                    "each segment requires source_in/source_out with source_out > source_in",
                    "semantic decisions must come from the external agent; measured boundaries must remain grounded in evidence",
                ],
            },
            "match": {
                "artifact": "match_proposal",
                "type": "object",
                "required": ["matches"],
                "rules": [
                    "matches must be a non-empty array",
                    "every match requires reference_segment_id, footage_segment_id, match_class, scores, and rationale",
                    "match_class must be good, acceptable, or fallback",
                    "fallback choices must remain explicit",
                ],
            },
            "render": {
                "artifact": "render_plan",
                "type": "object",
                "required": ["runtime_approved", "render_runtime"],
                "rules": ["runtime_approved must be true", "render_runtime must be ffmpeg, remotion, or hyperframes"],
            },
            "qc": {
                "artifact": "qc_bundle",
                "type": "object",
                "required": ["qc_report", "decision_log"],
                "rules": ["qc_report and decision_log must both be JSON objects"],
            },
        }
        if stage not in contracts:
            raise ValueError(f"stage {stage!r} does not expose an agent handoff contract")
        return contracts[stage]

    def _validate(self, stage: str, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValueError("handoff response must be a JSON object")
        if stage == "footage":
            clips = payload.get("clips")
            if not isinstance(clips, list) or not clips:
                raise ValueError("footage handoff requires non-empty clips[]")
            for clip in clips:
                if not isinstance(clip, dict) or not str(clip.get("path") or "").strip():
                    raise ValueError("each footage clip requires path")
                segments = clip.get("segments")
                if not isinstance(segments, list) or not segments:
                    raise ValueError("each footage clip requires non-empty segments[]")
                for segment in segments:
                    if not isinstance(segment, dict):
                        raise ValueError("footage segments must be objects")
                    start = segment.get("source_in")
                    end = segment.get("source_out")
                    if (
                        isinstance(start, bool)
                        or isinstance(end, bool)
                        or not isinstance(start, (int, float))
                        or not isinstance(end, (int, float))
                        or start < 0
                        or end <= start
                    ):
                        raise ValueError("each footage segment requires valid source_in/source_out")
        elif stage == "match":
            matches = payload.get("matches")
            if not isinstance(matches, list) or not matches:
                raise ValueError("match handoff requires non-empty matches[]")
            for item in matches:
                if not isinstance(item, dict):
                    raise ValueError("matches must contain objects")
                for field in ("reference_segment_id", "footage_segment_id", "rationale"):
                    if not str(item.get(field) or "").strip():
                        raise ValueError(f"each match requires {field}")
                if item.get("match_class") not in {"good", "acceptable", "fallback"}:
                    raise ValueError("match_class must be good, acceptable, or fallback")
                if not isinstance(item.get("scores"), dict):
                    raise ValueError("each match requires scores object")
        elif stage == "render":
            if payload.get("runtime_approved") is not True:
                raise ValueError("render handoff requires runtime_approved=true")
            if payload.get("render_runtime") not in {"ffmpeg", "remotion", "hyperframes"}:
                raise ValueError("invalid render_runtime")
        elif stage == "qc":
            if not isinstance(payload.get("qc_report"), dict) or not isinstance(payload.get("decision_log"), dict):
                raise ValueError("qc handoff requires qc_report and decision_log objects")
        else:
            raise ValueError(f"stage {stage!r} does not accept an agent handoff submission")

    def _required_artifact(self, stage: str) -> str:
        if stage == "qc":
            return "qc_bundle"
        return str(self._output_contract(stage)["artifact"])

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _handoff_id(package: dict[str, Any]) -> str:
        canonical = json.dumps(package, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()[:16]

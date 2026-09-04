from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from moon.agent_bridge import AgentBridgeService
from moon.evidence import SampledFrameEvidenceStore
from moon.handoff import AgentHandoffService
from moon.media.frames import sample_frames
from moon.media.inspection import resolve_project_source
from moon.runner.pipeline import PipelineRunner


CONNECTOR_VERSION = "1.2"
_IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class AgentConnectorService:
    """Thin agent-neutral tool surface over Moon Local.

    This service exposes deterministic project state, evidence access, frame sampling,
    and validated semantic submission. It never generates semantic decisions itself.
    """

    def __init__(self, runner: PipelineRunner) -> None:
        self.runner = runner

    def manifest(self) -> dict[str, Any]:
        return {
            "name": "moon-local",
            "version": CONNECTOR_VERSION,
            "transport": "stdin_stdout_json",
            "project_root": str(self.runner.project.root),
            "semantic_owner": "external_agent",
            "tools": [
                {"name": "moon.status", "input": {}},
                {"name": "moon.next", "input": {"max_steps": "integer?"}},
                {"name": "moon.handoff", "input": {"stage": "string?"}},
                {"name": "moon.evidence.list", "input": {"stage": "string?"}},
                {"name": "moon.evidence.read_json", "input": {"path": "project-relative string"}},
                {"name": "moon.evidence.read_image", "input": {"path": "project-relative image path"}},
                {"name": "moon.evidence.clear_sampled", "input": {}},
                {
                    "name": "moon.frames.sample",
                    "input": {
                        "source": "project-relative video path",
                        "start_seconds": "number",
                        "end_seconds": "number",
                        "count": "integer?",
                        "width": "integer?",
                    },
                },
                {
                    "name": "moon.submit",
                    "input": {"stage": "string", "payload": "object", "auto_next": "boolean?"},
                },
            ],
            "invariants": [
                "Moon does not generate semantic/editorial decisions.",
                "Evidence reads are restricted to the project root.",
                "Image evidence is returned as base64 bytes with an explicit MIME type.",
                "Frame sampling is deterministic and reads original local media.",
                "Sampled frame provenance is append-only and scoped to the active stage and revision.",
                "Semantic submissions are validated before persistence.",
            ],
        }

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        tool = request.get("tool")
        args = request.get("arguments", {})
        if not isinstance(tool, str):
            raise ValueError("connector request requires string field 'tool'")
        if not isinstance(args, dict):
            raise ValueError("connector request field 'arguments' must be an object")

        if tool == "moon.status":
            return self.runner.status()
        if tool == "moon.next":
            max_steps = args.get("max_steps", 20)
            if isinstance(max_steps, bool) or not isinstance(max_steps, int):
                raise ValueError("moon.next max_steps must be an integer")
            return AgentBridgeService(self.runner).next(max_steps=max_steps)
        if tool == "moon.handoff":
            stage = args.get("stage")
            if stage is not None and not isinstance(stage, str):
                raise ValueError("moon.handoff stage must be a string when provided")
            return AgentHandoffService(self.runner).package(stage)
        if tool == "moon.evidence.list":
            return self._evidence_list(args.get("stage"))
        if tool == "moon.evidence.read_json":
            path = args.get("path")
            if not isinstance(path, str):
                raise ValueError("moon.evidence.read_json requires string path")
            return self._read_json(path)
        if tool == "moon.evidence.read_image":
            path = args.get("path")
            if not isinstance(path, str):
                raise ValueError("moon.evidence.read_image requires string path")
            return self._read_image(path)
        if tool == "moon.evidence.clear_sampled":
            stage = self._active_evidence_stage()
            return self._sampled_store().clear(stage)
        if tool == "moon.frames.sample":
            return self._sample_frames(args)
        if tool == "moon.submit":
            stage = args.get("stage")
            payload = args.get("payload")
            auto_next = args.get("auto_next", True)
            if not isinstance(stage, str) or not isinstance(payload, dict):
                raise ValueError("moon.submit requires string stage and object payload")
            if not isinstance(auto_next, bool):
                raise ValueError("moon.submit auto_next must be boolean")
            accepted = AgentHandoffService(self.runner).submit(stage, payload)
            if not auto_next:
                return {"status": "accepted", "submission": accepted}
            return {
                "status": "accepted_and_advanced",
                "submission": accepted,
                "next": AgentBridgeService(self.runner).next(),
            }
        raise ValueError(f"unknown Moon connector tool: {tool!r}")

    def _evidence_list(self, stage: Any) -> dict[str, Any]:
        current = self.runner.state.next_stage()
        if stage is None:
            stage = current
        if not isinstance(stage, str):
            raise ValueError("moon.evidence.list stage must be a string when provided")
        if stage != current:
            raise ValueError(f"evidence may only be listed for current stage {current!r}")
        handoff = AgentHandoffService(self.runner).package(stage)
        evidence = handoff.get("inputs", {}).get("evidence", {})
        files = evidence.get("files", []) if isinstance(evidence, dict) else []
        sampled = self._sampled_store().exported(stage)
        sampled_by_path: dict[Path, dict[str, Any]] = {}
        for group in sampled["groups"]:
            for frame in group.get("frames") or []:
                sampled_by_path[Path(str(frame["absolute_path"])).resolve()] = {
                    "evidence_type": "sampled_frame",
                    "stage": stage,
                    "pipeline_revision": sampled["pipeline_revision"],
                    "sampling_group_id": group["group_id"],
                    "timestamp_seconds": frame["timestamp_seconds"],
                    "source": group["source"],
                    "request": group["request"],
                }
        registry_path = Path(sampled["registry_path"]).resolve()
        normalized = []
        root = self.runner.project.root.resolve()
        for raw in files:
            path = Path(raw).resolve()
            self._assert_inside_project(path)
            item = {
                "path": str(path.relative_to(root)),
                "absolute_path": str(path),
                "suffix": path.suffix.lower(),
                "kind": self._kind(path),
            }
            if path == registry_path:
                item.update({"kind": "sampled_frame_registry", "evidence_type": "sampled_frame_registry"})
            elif path in sampled_by_path:
                item.update({"kind": "sampled_frame", **sampled_by_path[path]})
            normalized.append(item)
        return {
            "stage": stage,
            "files": normalized,
            "sampled_frame_groups": sampled["groups"],
            "sampled_frame_count": sampled["frame_count"],
        }

    def _read_json(self, raw_path: str) -> dict[str, Any]:
        path = self._project_path(raw_path)
        if path.suffix.lower() != ".json":
            raise ValueError("moon.evidence.read_json only reads .json evidence")
        if not path.is_file():
            raise FileNotFoundError(str(path))
        data = json.loads(path.read_text(encoding="utf-8"))
        return {"path": str(path), "data": data}

    def _read_image(self, raw_path: str) -> dict[str, Any]:
        path = self._project_path(raw_path)
        suffix = path.suffix.lower()
        mime_type = _IMAGE_MIME_TYPES.get(suffix)
        if mime_type is None:
            raise ValueError("moon.evidence.read_image only reads jpg, jpeg, png, or webp evidence")
        if not path.is_file():
            raise FileNotFoundError(str(path))
        raw = path.read_bytes()
        return {
            "path": str(path),
            "mime_type": mime_type,
            "size_bytes": len(raw),
            "data_base64": base64.b64encode(raw).decode("ascii"),
        }

    def _sample_frames(self, args: dict[str, Any]) -> dict[str, Any]:
        source = args.get("source")
        if not isinstance(source, str):
            raise ValueError("moon.frames.sample requires string source")
        start = self._number(args.get("start_seconds"), "start_seconds")
        end = self._number(args.get("end_seconds"), "end_seconds")
        count = args.get("count", 8)
        width = args.get("width", 320)
        if isinstance(count, bool) or not isinstance(count, int):
            raise ValueError("moon.frames.sample count must be an integer")
        if isinstance(width, bool) or not isinstance(width, int):
            raise ValueError("moon.frames.sample width must be an integer")
        source_path = resolve_project_source(self.runner.project, source)
        stage = self._active_evidence_stage()
        store = self._sampled_store()
        group_id = store.group_id(
            stage,
            source_path,
            start_seconds=start,
            end_seconds=end,
            count=count,
            width=width,
        )
        cache = (
            self.runner.project.cache_dir
            / "connector-frames"
            / stage
            / f"revision_{self.runner.state.revision:03d}"
            / group_id
        )
        result = sample_frames(
            source_path,
            cache,
            start_seconds=start,
            end_seconds=end,
            count=count,
            width=width,
        )
        registered = store.register(
            stage,
            result,
            group_id=group_id,
            clip_id=self._clip_id_for_source(source_path),
        )
        return {
            **result,
            "stage": stage,
            "pipeline_revision": self.runner.state.revision,
            "sampling_group_id": group_id,
            "evidence_registry_path": str(store.registry_path(stage)),
            "evidence_registered": True,
            "provenance": registered,
        }

    def _active_evidence_stage(self) -> str:
        stage = self.runner.state.next_stage()
        if stage is None:
            raise ValueError("sampled evidence requires an active Moon stage")
        if not self.runner.artifacts.exists(f"{stage}_agent_task"):
            raise ValueError(f"sampled evidence requires an active semantic handoff for stage {stage!r}")
        return stage

    def _sampled_store(self) -> SampledFrameEvidenceStore:
        return SampledFrameEvidenceStore(self.runner.project, self.runner.state.revision)

    def _clip_id_for_source(self, source: Path) -> str | None:
        for artifact_name in ("footage_profiles_scaffold", "footage_profiles"):
            if not self.runner.artifacts.exists(artifact_name):
                continue
            for clip in self.runner.artifacts.read(artifact_name).get("clips") or []:
                if not isinstance(clip, dict) or not clip.get("path"):
                    continue
                raw = Path(str(clip["path"]))
                candidate = raw if raw.is_absolute() else self.runner.project.root / raw
                if candidate.resolve() == source.resolve():
                    return str(clip.get("clip_id") or "") or None
        return None

    def _project_path(self, raw_path: str) -> Path:
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.runner.project.root / candidate
        candidate = candidate.resolve()
        self._assert_inside_project(candidate)
        return candidate

    def _assert_inside_project(self, path: Path) -> None:
        root = self.runner.project.root.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("connector evidence path must stay inside project root") from exc

    @staticmethod
    def _kind(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".json":
            return "json"
        if suffix in _IMAGE_MIME_TYPES:
            return "image"
        if suffix in {".mp4", ".mov", ".mkv", ".webm"}:
            return "video"
        return "file"

    @staticmethod
    def _number(value: Any, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"moon.frames.sample requires numeric {field}")
        return float(value)

def parse_connector_request(raw: str) -> dict[str, Any]:
    if not raw.strip():
        raise ValueError("connector-call requires one JSON object on stdin")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("connector-call stdin must be a JSON object")
    return payload

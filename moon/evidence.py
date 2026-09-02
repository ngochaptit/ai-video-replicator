from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from moon.core.project import MoonProject


SAMPLED_EVIDENCE_SCHEMA_VERSION = 1
_SAFE_STAGE = re.compile(r"^[A-Za-z0-9_-]+$")


class SampledFrameEvidenceStore:
    """Append-only, stage/revision-scoped provenance for sampled frame evidence."""

    def __init__(self, project: MoonProject, pipeline_revision: int) -> None:
        self.project = project
        self.pipeline_revision = int(pipeline_revision)

    def registry_path(self, stage: str) -> Path:
        self._validate_stage(stage)
        return (
            self.project.evidence_dir
            / "sampled"
            / stage
            / f"revision_{self.pipeline_revision:03d}"
            / "events.jsonl"
        )

    def group_id(
        self,
        stage: str,
        source: Path,
        *,
        start_seconds: float,
        end_seconds: float,
        count: int,
        width: int,
    ) -> str:
        canonical = {
            "schema_version": SAMPLED_EVIDENCE_SCHEMA_VERSION,
            "stage": stage,
            "pipeline_revision": self.pipeline_revision,
            "source_path": self._relative(source),
            "request": {
                "start_seconds": round(float(start_seconds), 6),
                "end_seconds": round(float(end_seconds), 6),
                "count": count,
                "width": width,
            },
        }
        raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:20]

    def register(
        self,
        stage: str,
        result: dict[str, Any],
        *,
        group_id: str,
        clip_id: str | None,
    ) -> dict[str, Any]:
        source = Path(str(result["source"])).resolve()
        frames = []
        for item in result.get("frames") or []:
            timestamp = float(item["timestamp_seconds"])
            frames.append({
                "timestamp_seconds": round(timestamp, 6),
                "path": self._relative(Path(str(item["path"]))),
            })
        event = {
            "schema_version": SAMPLED_EVIDENCE_SCHEMA_VERSION,
            "event_type": "sampled_frame_group",
            "stage": stage,
            "pipeline_revision": self.pipeline_revision,
            "group_id": group_id,
            "sampling_method": "ffmpeg_single_frame_seek_v1",
            "source": {
                "clip_id": clip_id,
                "path": self._relative(source),
            },
            "request": {
                "start_seconds": round(float(result["start_seconds"]), 6),
                "end_seconds": round(float(result["end_seconds"]), 6),
                "count": int(result["count"]),
                "width": int(result["width"]),
            },
            "frames": frames,
        }
        self._append(stage, event)
        return event

    def active(self, stage: str) -> dict[str, Any]:
        groups: dict[str, dict[str, Any]] = {}
        for event in self._events(stage):
            if event["event_type"] == "clear_sampled_frames":
                groups.clear()
            elif event["event_type"] == "sampled_frame_group":
                groups[str(event["group_id"])] = event
        active_groups = list(groups.values())
        return {
            "schema_version": SAMPLED_EVIDENCE_SCHEMA_VERSION,
            "stage": stage,
            "pipeline_revision": self.pipeline_revision,
            "registry_path": str(self.registry_path(stage)),
            "groups": active_groups,
            "frame_count": sum(len(group.get("frames") or []) for group in active_groups),
        }

    def clear(self, stage: str) -> dict[str, Any]:
        active = self.active(stage)
        group_ids = [str(group["group_id"]) for group in active["groups"]]
        registry = self.registry_path(stage)
        marker_source = f"{stage}:{self.pipeline_revision}:{registry.stat().st_size if registry.exists() else 0}"
        clear_id = hashlib.sha256(marker_source.encode("utf-8")).hexdigest()[:20]
        event = {
            "schema_version": SAMPLED_EVIDENCE_SCHEMA_VERSION,
            "event_type": "clear_sampled_frames",
            "stage": stage,
            "pipeline_revision": self.pipeline_revision,
            "clear_id": clear_id,
            "cleared_group_ids": group_ids,
        }
        self._append(stage, event)
        return {
            "stage": stage,
            "pipeline_revision": self.pipeline_revision,
            "clear_id": clear_id,
            "cleared_groups": len(group_ids),
            "cleared_frames": int(active["frame_count"]),
            "images_deleted": False,
            "registry_path": str(registry),
            "next_action": "Call moon.frames.sample to start a new sampled evidence set for this stage.",
        }

    def absolute_path(self, stored_path: str) -> Path:
        candidate = (self.project.root / stored_path).resolve()
        self._assert_inside_project(candidate)
        return candidate

    def exported(self, stage: str) -> dict[str, Any]:
        active = self.active(stage)
        groups = []
        for stored in active["groups"]:
            group = dict(stored)
            source = dict(group["source"])
            source["absolute_path"] = str(self.absolute_path(str(source["path"])))
            group["source"] = source
            group["frames"] = [
                {
                    **frame,
                    "absolute_path": str(self.absolute_path(str(frame["path"]))),
                }
                for frame in group.get("frames") or []
            ]
            groups.append(group)
        return {**active, "groups": groups}

    def _events(self, stage: str) -> list[dict[str, Any]]:
        registry = self.registry_path(stage)
        if not registry.is_file():
            return []
        events = []
        for line_number, raw in enumerate(registry.read_text(encoding="utf-8").splitlines(), start=1):
            if not raw.strip():
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid sampled evidence event at {registry}:{line_number}") from exc
            if not isinstance(event, dict):
                raise ValueError(f"sampled evidence event must be an object at {registry}:{line_number}")
            if event.get("schema_version") != SAMPLED_EVIDENCE_SCHEMA_VERSION:
                raise ValueError(f"unsupported sampled evidence schema at {registry}:{line_number}")
            if event.get("stage") != stage or event.get("pipeline_revision") != self.pipeline_revision:
                raise ValueError(f"sampled evidence scope mismatch at {registry}:{line_number}")
            event_type = event.get("event_type")
            if event_type not in {"sampled_frame_group", "clear_sampled_frames"}:
                raise ValueError(f"unknown sampled evidence event at {registry}:{line_number}")
            if event_type == "sampled_frame_group":
                self.absolute_path(str(event["source"]["path"]))
                for frame in event.get("frames") or []:
                    self.absolute_path(str(frame["path"]))
            events.append(event)
        return events

    def _append(self, stage: str, event: dict[str, Any]) -> None:
        registry = self.registry_path(stage)
        registry.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with registry.open("a", encoding="utf-8", newline="") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

    def _relative(self, path: Path) -> str:
        resolved = path.expanduser().resolve()
        self._assert_inside_project(resolved)
        return resolved.relative_to(self.project.root.resolve()).as_posix()

    def _assert_inside_project(self, path: Path) -> None:
        try:
            path.relative_to(self.project.root.resolve())
        except ValueError as exc:
            raise ValueError("sampled evidence path must stay inside project root") from exc

    @staticmethod
    def _validate_stage(stage: str) -> None:
        if not isinstance(stage, str) or _SAFE_STAGE.fullmatch(stage) is None:
            raise ValueError("sampled evidence stage must use letters, numbers, '-' or '_'")

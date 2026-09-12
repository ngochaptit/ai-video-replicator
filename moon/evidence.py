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
        self._fingerprints: dict[Path, str] = {}

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
            "source_sha256": self.source_fingerprint(source),
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
        sample_kind: str = "manual",
        handoff_revision: int | None = None,
    ) -> dict[str, Any]:
        source = Path(str(result["source"])).resolve()
        frames = []
        for item in result.get("frames") or []:
            timestamp = float(item["timestamp_seconds"])
            frames.append(
                {
                    "timestamp_seconds": round(timestamp, 6),
                    "path": self._relative(Path(str(item["path"]))),
                }
            )
        event = {
            "schema_version": SAMPLED_EVIDENCE_SCHEMA_VERSION,
            "event_type": "sampled_frame_group",
            "stage": stage,
            "pipeline_revision": self.pipeline_revision,
            "group_id": group_id,
            "sampling_method": "ffmpeg_single_frame_seek_v1",
            "sample_kind": sample_kind,
            "handoff_revision": handoff_revision,
            "checkpoint_state": "completed",
            "source": {
                "clip_id": clip_id,
                "path": self._relative(source),
                "sha256": self.source_fingerprint(source),
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

    def reusable_group_ids(self, stage: str) -> set[str]:
        """Return completed groups whose source and materialized frames still match."""
        return {
            str(group["group_id"])
            for group in self.active(stage)["groups"]
            if self._is_reusable(group)
        }

    def available(self, stage: str) -> dict[str, Any]:
        """Export readable evidence, normalizing legacy groups without mutating history."""
        active = self.active(stage)
        normalized = [
            self._normalize_legacy_group(group)
            for group in active["groups"]
            if self._is_available(group)
        ]
        groups = self._dedupe_equivalent_groups(normalized)
        exported = self._export_groups(groups)
        return {
            **active,
            "groups": exported,
            "frame_count": sum(len(group.get("frames") or []) for group in exported),
        }

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
        marker_source = (
            f"{stage}:{self.pipeline_revision}:"
            f"{registry.stat().st_size if registry.exists() else 0}"
        )
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
            "next_action": (
                "Call moon.frames.sample to start a new sampled evidence set for this stage."
            ),
        }

    def absolute_path(self, stored_path: str) -> Path:
        candidate = (self.project.root / stored_path).resolve()
        self._assert_inside_project(candidate)
        return candidate

    def exported(self, stage: str) -> dict[str, Any]:
        active = self.active(stage)
        return {**active, "groups": self._export_groups(active["groups"])}

    def source_fingerprint(self, source: Path) -> str:
        return self._file_fingerprint(source)

    def _file_fingerprint(self, path: Path) -> str:
        resolved = path.expanduser().resolve(strict=True)
        self._assert_inside_project(resolved)
        cached = self._fingerprints.get(resolved)
        if cached is not None:
            return cached
        digest = hashlib.sha256()
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        value = digest.hexdigest()
        self._fingerprints[resolved] = value
        return value

    def clear_fingerprint_cache(self) -> None:
        self._fingerprints.clear()

    def _normalize_legacy_group(self, stored: dict[str, Any]) -> dict[str, Any]:
        group = dict(stored)
        sample_kind = str(group.get("sample_kind") or "").strip().lower()
        if sample_kind in {"coarse", "dense_refinement"}:
            return group

        handoff_revision = group.get("handoff_revision")
        frame_paths = [
            str(frame.get("path") or "").replace("\\", "/")
            for frame in group.get("frames") or []
        ]
        legacy_refinement = (
            (
                isinstance(handoff_revision, int)
                and not isinstance(handoff_revision, bool)
                and handoff_revision > 0
            )
            or any("/refinement_" in f"/{path}" for path in frame_paths)
        )
        if legacy_refinement:
            group["sample_kind"] = "dense_refinement"
        elif sample_kind:
            group["sample_kind"] = sample_kind
        else:
            group["sample_kind"] = "manual"
        return group

    def _dedupe_equivalent_groups(
        self, groups: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Collapse duplicate migrated sampling groups by measured frame content."""
        chosen: dict[str, dict[str, Any]] = {}
        for group in groups:
            signature = self._equivalent_group_signature(group)
            current = chosen.get(signature)
            if current is None or self._group_preference(group) < self._group_preference(
                current
            ):
                chosen[signature] = group
        return list(chosen.values())

    @staticmethod
    def _group_preference(group: dict[str, Any]) -> tuple[int, str]:
        kind = str(group.get("sample_kind") or "manual")
        rank = {
            "coarse": 0,
            "dense_refinement": 0,
            "manual": 1,
        }.get(kind, 2)
        return rank, str(group.get("group_id") or "")

    def _equivalent_group_signature(self, group: dict[str, Any]) -> str:
        source = group.get("source") or {}
        request = group.get("request") or {}
        kind = str(group.get("sample_kind") or "manual")
        family = "refinement" if kind == "dense_refinement" else "coarse"
        source_path = str(source.get("path") or "")
        try:
            source_sha256 = self.source_fingerprint(
                self.absolute_path(source_path)
            )
        except (OSError, ValueError):
            source_sha256 = str(source.get("sha256") or "")
        frames = []
        for frame in group.get("frames") or []:
            frame_path = self.absolute_path(str(frame.get("path") or ""))
            frames.append(
                {
                    "timestamp_seconds": round(
                        float(frame.get("timestamp_seconds") or 0.0), 6
                    ),
                    "sha256": self._file_fingerprint(frame_path),
                }
            )
        canonical = {
            "family": family,
            "source": {
                "clip_id": source.get("clip_id"),
                "path": source_path,
                "sha256": source_sha256,
            },
            "window": {
                "start_seconds": request.get("start_seconds"),
                "end_seconds": request.get("end_seconds"),
            },
            "frames": frames,
        }
        raw = json.dumps(
            canonical, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _export_groups(self, stored_groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups = []
        for stored in stored_groups:
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
        return groups

    def _frames_exist(self, group: dict[str, Any]) -> bool:
        frames = group.get("frames") or []
        return bool(frames) and all(
            self.absolute_path(str(frame.get("path") or "")).is_file()
            for frame in frames
        )

    def _is_reusable(self, group: dict[str, Any]) -> bool:
        if group.get("checkpoint_state") != "completed" or not self._frames_exist(group):
            return False
        source = group.get("source") or {}
        expected = str(source.get("sha256") or "")
        if not expected:
            return False
        try:
            return (
                self.source_fingerprint(
                    self.absolute_path(str(source.get("path") or ""))
                )
                == expected
            )
        except (OSError, ValueError):
            return False

    def _is_available(self, group: dict[str, Any]) -> bool:
        if not self._frames_exist(group):
            return False
        source = group.get("source") or {}
        expected = str(source.get("sha256") or "")
        if not expected:
            return True
        try:
            return (
                self.source_fingerprint(
                    self.absolute_path(str(source.get("path") or ""))
                )
                == expected
            )
        except (OSError, ValueError):
            return False

    def _events(self, stage: str) -> list[dict[str, Any]]:
        registry = self.registry_path(stage)
        if not registry.is_file():
            return []
        events = []
        for line_number, raw in enumerate(
            registry.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not raw.strip():
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid sampled evidence event at {registry}:{line_number}"
                ) from exc
            if not isinstance(event, dict):
                raise ValueError(
                    f"sampled evidence event must be an object at {registry}:{line_number}"
                )
            if event.get("schema_version") != SAMPLED_EVIDENCE_SCHEMA_VERSION:
                raise ValueError(
                    f"unsupported sampled evidence schema at {registry}:{line_number}"
                )
            if (
                event.get("stage") != stage
                or event.get("pipeline_revision") != self.pipeline_revision
            ):
                raise ValueError(
                    f"sampled evidence scope mismatch at {registry}:{line_number}"
                )
            event_type = event.get("event_type")
            if event_type not in {"sampled_frame_group", "clear_sampled_frames"}:
                raise ValueError(
                    f"unknown sampled evidence event at {registry}:{line_number}"
                )
            if event_type == "sampled_frame_group":
                self.absolute_path(str(event["source"]["path"]))
                for frame in event.get("frames") or []:
                    self.absolute_path(str(frame["path"]))
            events.append(event)
        return events

    def _append(self, stage: str, event: dict[str, Any]) -> None:
        registry = self.registry_path(stage)
        registry.parent.mkdir(parents=True, exist_ok=True)
        encoded = (
            json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
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
            raise ValueError(
                "sampled evidence stage must use letters, numbers, '-' or '_'"
            )
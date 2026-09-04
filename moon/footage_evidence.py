from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from moon.core.project import MoonProject
from moon.evidence import SampledFrameEvidenceStore
from moon.media.frames import sample_frames
from moon.media.inspection import resolve_project_source

DEFAULT_TARGET_SPACING_SECONDS = 4.0
DEFAULT_MAX_FRAMES_PER_CLIP = 120
DEFAULT_MAX_GROUP_FRAMES = 24
DEFAULT_WIDTH = 320


class FootageEvidencePlanner:
    """Deterministically seed dense footage evidence without making semantic choices."""

    def __init__(self, project: MoonProject, pipeline_revision: int) -> None:
        self.project = project
        self.pipeline_revision = int(pipeline_revision)
        self.store = SampledFrameEvidenceStore(project, pipeline_revision)

    @staticmethod
    def plan_clip(
        duration_seconds: float,
        *,
        target_spacing_seconds: float = DEFAULT_TARGET_SPACING_SECONDS,
        max_frames_per_clip: int = DEFAULT_MAX_FRAMES_PER_CLIP,
        max_group_frames: int = DEFAULT_MAX_GROUP_FRAMES,
    ) -> dict[str, Any]:
        duration = float(duration_seconds)
        if duration <= 0:
            return {"duration_seconds": duration, "spacing_seconds": None, "groups": [], "estimated_unique_frames": 0}
        if target_spacing_seconds <= 0:
            raise ValueError("target_spacing_seconds must be positive")
        if max_frames_per_clip < 2:
            raise ValueError("max_frames_per_clip must be at least 2")
        if max_group_frames < 2 or max_group_frames > 24:
            raise ValueError("max_group_frames must be between 2 and 24")

        spacing = max(float(target_spacing_seconds), duration / max(1, max_frames_per_clip - 1))
        max_span = spacing * (max_group_frames - 1)
        groups: list[dict[str, Any]] = []
        cursor = 0.0
        while cursor < duration - 1e-9:
            end = min(duration, cursor + max_span)
            span = end - cursor
            count = max(2, min(max_group_frames, int(math.ceil(span / spacing)) + 1))
            groups.append({
                "start_seconds": round(cursor, 6),
                "end_seconds": round(end, 6),
                "count": count,
            })
            if end >= duration - 1e-9:
                break
            cursor = end

        unique_estimate = sum(group["count"] for group in groups) - max(0, len(groups) - 1)
        return {
            "duration_seconds": round(duration, 6),
            "spacing_seconds": round(spacing, 6),
            "groups": groups,
            "estimated_unique_frames": unique_estimate,
        }

    def seed(self, scaffold: dict[str, Any]) -> dict[str, Any]:
        active_ids = {str(group["group_id"]) for group in self.store.active("footage")["groups"]}
        seeded_groups = 0
        skipped_groups = 0
        errors: list[str] = []
        plans: list[dict[str, Any]] = []

        for clip in scaffold.get("clips") or []:
            clip_id = str(clip.get("clip_id") or "")
            duration = float(clip.get("duration_seconds") or 0.0)
            raw_path = str(clip.get("path") or "")
            if not clip_id or not raw_path or duration <= 0:
                continue
            try:
                source = resolve_project_source(self.project, raw_path)
            except (OSError, ValueError) as exc:
                errors.append(f"{clip_id}: could not resolve source for adaptive evidence: {exc}")
                continue

            plan = self.plan_clip(duration)
            plans.append({"clip_id": clip_id, "source": raw_path, **plan})
            for group in plan["groups"]:
                group_id = self.store.group_id(
                    "footage",
                    source,
                    start_seconds=float(group["start_seconds"]),
                    end_seconds=float(group["end_seconds"]),
                    count=int(group["count"]),
                    width=DEFAULT_WIDTH,
                )
                if group_id in active_ids:
                    skipped_groups += 1
                    continue
                cache = (
                    self.project.cache_dir
                    / "connector-frames"
                    / "footage"
                    / f"revision_{self.pipeline_revision:03d}"
                    / group_id
                )
                try:
                    result = sample_frames(
                        source,
                        cache,
                        start_seconds=float(group["start_seconds"]),
                        end_seconds=float(group["end_seconds"]),
                        count=int(group["count"]),
                        width=DEFAULT_WIDTH,
                    )
                    self.store.register("footage", result, group_id=group_id, clip_id=clip_id)
                    active_ids.add(group_id)
                    seeded_groups += 1
                except Exception as exc:  # evidence boost must not destroy the base scaffold
                    errors.append(f"{clip_id}: adaptive evidence group {group_id} failed: {exc}")

        exported = self.store.exported("footage")
        summary = self.coverage_summary(scaffold)
        return {
            "policy": "adaptive_uniform_seed_v1",
            "target_spacing_seconds": DEFAULT_TARGET_SPACING_SECONDS,
            "max_frames_per_clip": DEFAULT_MAX_FRAMES_PER_CLIP,
            "seeded_groups": seeded_groups,
            "skipped_existing_groups": skipped_groups,
            "frame_count": exported["frame_count"],
            "plans": plans,
            "coverage": summary,
            "errors": errors,
        }

    def evidence_catalog(self, scaffold: dict[str, Any]) -> list[dict[str, Any]]:
        valid_clip_ids = {str(clip.get("clip_id") or "") for clip in scaffold.get("clips") or []}
        entries: list[dict[str, Any]] = []
        for group in self.store.active("footage")["groups"]:
            source = group.get("source") or {}
            clip_id = str(source.get("clip_id") or "")
            if clip_id not in valid_clip_ids:
                continue
            for frame in group.get("frames") or []:
                stored_path = str(frame.get("path") or "")
                if not stored_path:
                    continue
                entries.append({
                    "clip_id": clip_id,
                    "path": str(self.store.absolute_path(stored_path)),
                    "timestamp": round(float(frame["timestamp_seconds"]), 6),
                    "scene_index": None,
                })
        unique = {(item["clip_id"], item["path"], item["timestamp"]): item for item in entries}
        return sorted(unique.values(), key=lambda item: (item["clip_id"], item["timestamp"], item["path"]))

    def coverage_summary(self, scaffold: dict[str, Any]) -> list[dict[str, Any]]:
        by_clip: dict[str, list[float]] = {}
        for item in self.evidence_catalog(scaffold):
            by_clip.setdefault(item["clip_id"], []).append(float(item["timestamp"]))
        summary: list[dict[str, Any]] = []
        for clip in scaffold.get("clips") or []:
            clip_id = str(clip.get("clip_id") or "")
            duration = float(clip.get("duration_seconds") or 0.0)
            values = sorted(set(by_clip.get(clip_id, [])))
            gaps: list[float] = []
            if values:
                gaps.append(max(0.0, values[0]))
                gaps.extend(max(0.0, b - a) for a, b in zip(values, values[1:]))
                gaps.append(max(0.0, duration - values[-1]))
            summary.append({
                "clip_id": clip_id,
                "duration_seconds": round(duration, 6),
                "sampled_frame_count": len(values),
                "max_gap_seconds": round(max(gaps), 6) if gaps else None,
            })
        return summary

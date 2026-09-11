from __future__ import annotations

from typing import Any

from jsonschema import ValidationError, validate

from moon.evidence import SampledFrameEvidenceStore
from moon.footage_evidence import FootageEvidencePlanner
from moon.media.frames import sample_frames
from moon.media.inspection import resolve_project_source
from moon.runner.pipeline import PipelineRunner


REFINEMENT_FRAME_COUNT = 9
REFINEMENT_FRAME_WIDTH = 640
MAX_REFINEMENT_REQUESTS = 20
MAX_REFINEMENT_WINDOW_SECONDS = 30.0

FOOTAGE_REFINEMENT_REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["artifact", "requests"],
    "properties": {
        "artifact": {"const": "footage_refinement_request"},
        "requests": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_REFINEMENT_REQUESTS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "clip_id",
                    "start_seconds",
                    "end_seconds",
                    "reason",
                ],
                "properties": {
                    "clip_id": {"type": "string", "minLength": 1},
                    "start_seconds": {"type": "number", "minimum": 0},
                    "end_seconds": {"type": "number", "exclusiveMinimum": 0},
                    "reason": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}


class FootageRefinementService:
    """Validate GPT refinement decisions and deterministically sample their windows."""

    def __init__(self, runner: PipelineRunner) -> None:
        self.runner = runner
        self.scaffold = runner.artifacts.read("footage_profiles_scaffold")
        self.store = SampledFrameEvidenceStore(
            runner.project, runner.state.revision
        )

    def validate(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            validate(payload, FOOTAGE_REFINEMENT_REQUEST_SCHEMA)
        except ValidationError as exc:
            raise ValueError(
                f"invalid footage_refinement_request: {exc.message}"
            ) from exc

        clips = {
            str(clip.get("clip_id") or ""): clip
            for clip in self.scaffold.get("clips") or []
            if isinstance(clip, dict) and clip.get("clip_id")
        }
        normalized: list[dict[str, Any]] = []
        identities: set[tuple[str, float, float]] = set()
        for item in payload["requests"]:
            clip_id = str(item["clip_id"]).strip()
            if clip_id not in clips:
                raise ValueError(
                    f"refinement clip_id {clip_id!r} is not in "
                    "footage_profiles_scaffold"
                )
            start = round(float(item["start_seconds"]), 6)
            end = round(float(item["end_seconds"]), 6)
            duration = float(clips[clip_id].get("duration_seconds") or 0.0)
            if end <= start:
                raise ValueError(
                    "each refinement window requires end_seconds > start_seconds"
                )
            if end - start > MAX_REFINEMENT_WINDOW_SECONDS:
                raise ValueError(
                    "refinement windows must be at most "
                    f"{MAX_REFINEMENT_WINDOW_SECONDS:g} seconds"
                )
            if end > duration + 1e-6:
                raise ValueError(
                    f"refinement window for {clip_id!r} exceeds measured "
                    f"duration {duration}"
                )
            reason = str(item["reason"]).strip()
            if not reason:
                raise ValueError(
                    "each refinement window requires a non-empty reason"
                )
            identity = (clip_id, start, end)
            if identity in identities:
                raise ValueError(
                    "duplicate footage refinement windows are not allowed"
                )
            identities.add(identity)
            normalized.append(
                {
                    "clip_id": clip_id,
                    "start_seconds": start,
                    "end_seconds": end,
                    "reason": reason,
                }
            )

        self._validate_active_batch_scope(normalized)
        return normalized

    def _validate_active_batch_scope(
        self, requests: list[dict[str, Any]]
    ) -> None:
        # Validate scope before sampling so an untrusted response cannot make Moon
        # inspect clips/ranges that were not part of the active GPT batch.
        from moon.footage_batches import FootageSemanticProgress

        progress = FootageSemanticProgress.open_existing(self.runner)
        active = progress.active_for_request() if progress else None
        if not active:
            return

        windows: dict[str, list[tuple[float, float]]] = {}
        for item in active.get("ranges") or []:
            windows.setdefault(str(item["clip_id"]), []).append(
                (
                    float(item["start_seconds"]),
                    float(item["end_seconds"]),
                )
            )
        for item in requests:
            start = float(item["start_seconds"])
            end = float(item["end_seconds"])
            if not any(
                start >= low - 1e-6 and end <= high + 1e-6
                for low, high in windows.get(str(item["clip_id"]), [])
            ):
                raise ValueError(
                    "footage refinement request is outside the active "
                    "semantic batch"
                )

    def sample(
        self,
        requests: list[dict[str, Any]],
        *,
        handoff_revision: int,
    ) -> dict[str, Any]:
        self.store.clear_fingerprint_cache()
        clips = {
            str(clip.get("clip_id") or ""): clip
            for clip in self.scaffold.get("clips") or []
            if isinstance(clip, dict) and clip.get("clip_id")
        }
        active_ids = self.store.reusable_group_ids("footage")
        groups: list[dict[str, Any]] = []
        sampled = 0
        skipped = 0
        for item in requests:
            clip = clips[item["clip_id"]]
            source = resolve_project_source(
                self.runner.project, str(clip.get("path") or "")
            )
            group_id = self.store.group_id(
                "footage",
                source,
                start_seconds=item["start_seconds"],
                end_seconds=item["end_seconds"],
                count=REFINEMENT_FRAME_COUNT,
                width=REFINEMENT_FRAME_WIDTH,
            )
            if group_id in active_ids:
                skipped += 1
            else:
                output_dir = (
                    self.runner.project.cache_dir
                    / "connector-frames"
                    / "footage"
                    / f"revision_{self.runner.state.revision:03d}"
                    / f"refinement_{handoff_revision:03d}"
                    / group_id
                )
                result = sample_frames(
                    source,
                    output_dir,
                    start_seconds=item["start_seconds"],
                    end_seconds=item["end_seconds"],
                    count=REFINEMENT_FRAME_COUNT,
                    width=REFINEMENT_FRAME_WIDTH,
                )
                self.store.register(
                    "footage",
                    result,
                    group_id=group_id,
                    clip_id=item["clip_id"],
                    sample_kind="dense_refinement",
                    handoff_revision=handoff_revision,
                )
                active_ids.add(group_id)
                sampled += 1
            groups.append({**item, "group_id": group_id})

        planner = FootageEvidencePlanner(
            self.runner.project, self.runner.state.revision
        )
        previous: dict[str, Any] = {}
        if self.runner.artifacts.exists("footage_evidence_catalog"):
            previous = self.runner.artifacts.read("footage_evidence_catalog")
        refinements = list(previous.get("refinements") or [])
        refinements.append(
            {
                "handoff_revision": handoff_revision,
                "requests": groups,
                "frame_count_per_window": REFINEMENT_FRAME_COUNT,
                "frame_width": REFINEMENT_FRAME_WIDTH,
            }
        )
        catalog = {
            "version": "1.0",
            "entries": planner.evidence_catalog(self.scaffold),
            "coverage": planner.coverage_summary(self.scaffold),
            "policy": str(
                previous.get("policy") or "adaptive_uniform_seed_v1"
            ),
            "refinements": refinements,
        }
        self.runner.artifacts.write("footage_evidence_catalog", catalog)
        return {
            "sampled_groups": sampled,
            "skipped_existing_groups": skipped,
            "groups": groups,
            "coverage": catalog["coverage"],
        }

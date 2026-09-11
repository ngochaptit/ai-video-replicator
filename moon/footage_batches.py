from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from moon.atomic import atomic_write_json
from moon.evidence import SampledFrameEvidenceStore
from moon.footage_evidence import FootageEvidencePlanner
from moon.runner.pipeline import PipelineRunner
from moon.semantic_contracts import _validate_footage


PROGRESS_VERSION = "1.1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


@dataclass(frozen=True)
class FootageBatchPolicy:
    max_clips: int = 3
    max_refinement_ranges: int = 3
    max_frames: int = 60
    max_evidence_bytes: int = 24 * 1024 * 1024

    def validate(self) -> None:
        if min(
            self.max_clips,
            self.max_refinement_ranges,
            self.max_frames,
            self.max_evidence_bytes,
        ) <= 0:
            raise ValueError("footage semantic batch limits must be positive")


class FootageSemanticProgress:
    """Durable, request-independent progress for bounded footage semantic batches."""

    def __init__(self, runner: PipelineRunner, policy: FootageBatchPolicy) -> None:
        policy.validate()
        self.runner = runner
        self.project = runner.project
        self.policy = policy
        self.path = self.project.moon_dir / "footage-semantic-progress.json"

    @classmethod
    def open_existing(cls, runner: PipelineRunner) -> FootageSemanticProgress | None:
        path = runner.project.moon_dir / "footage-semantic-progress.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return None
            policy = FootageBatchPolicy(**value["policy"])
            policy.validate()
            if (
                value.get("version") != PROGRESS_VERSION
                or int(value.get("pipeline_revision", -1)) != runner.state.revision
                or not isinstance(value.get("batches"), list)
            ):
                return None
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ):
            return None

        progress = cls(runner, policy)
        if not runner.artifacts.exists("footage_profiles_scaffold"):
            return None
        scaffold = runner.artifacts.read("footage_profiles_scaffold")
        if not scaffold.get("clips"):
            return None
        groups = progress._candidate_groups(kinds={"coarse", "manual"})
        if not groups:
            return None
        if value.get("identity_sha256") != progress._identity(scaffold, groups):
            return None
        return progress

    def ensure(self) -> dict[str, Any] | None:
        if not self.runner.artifacts.exists("footage_profiles_scaffold"):
            return None
        scaffold = self.runner.artifacts.read("footage_profiles_scaffold")
        if not scaffold.get("clips"):
            return None

        # Bootstrap semantic work from coarse/manual evidence only. Dense refinement
        # groups may pre-exist in migrated projects, but they are reusable measured
        # cache, not pre-approved semantic work. They become child batches only after
        # GPT requests refinement for a specific active parent batch.
        groups = self._candidate_groups(kinds={"coarse", "manual"})
        if not groups:
            return None

        identity = self._identity(scaffold, groups)
        current = self.load()
        if self._matches_current(current, identity):
            return current

        batches = self._pack(groups, start_index=1)
        progress = {
            "version": PROGRESS_VERSION,
            "artifact": "footage_semantic_progress",
            "pipeline_revision": self.runner.state.revision,
            "identity_sha256": identity,
            "policy": asdict(self.policy),
            "active_batch_id": None,
            "next_revision": self.runner.state.revision,
            "batches": batches,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
        }
        self.save(progress)
        return progress

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def save(self, progress: dict[str, Any]) -> None:
        progress["updated_at"] = _utc_now()
        _atomic_json(self.path, progress)

    def activate(self) -> dict[str, Any] | None:
        progress = self.ensure()
        if progress is None:
            return None
        active_id = progress.get("active_batch_id")
        if active_id:
            active = self._by_id(progress, str(active_id))
            if active.get("status") in {"pending", "waiting_agent", "waiting_gpt"}:
                return active
            progress["active_batch_id"] = None

        completed = {
            str(item["batch_id"])
            for item in progress["batches"]
            if item.get("status") == "completed"
        }
        candidates = [
            item
            for item in progress["batches"]
            if item.get("status") in {"pending", "blocked_refinement"}
            and set(item.get("blocked_by") or []).issubset(completed)
        ]
        if not candidates:
            self.save(progress)
            return None
        candidates.sort(
            key=lambda item: (
                0 if item.get("batch_type") == "refinement" else 1,
                int(item.get("sequence", 0)),
            )
        )
        active = candidates[0]
        if active.get("status") == "blocked_refinement":
            active["status"] = "pending"
        if active.get("revision") is None:
            active["revision"] = int(progress["next_revision"])
            progress["next_revision"] = int(progress["next_revision"]) + 1
        progress["active_batch_id"] = active["batch_id"]
        self.save(progress)
        return active

    def bind_request(
        self,
        batch_id: str,
        request_id: str,
        revision: int,
        evidence_hashes: list[str],
    ) -> None:
        progress = self.load()
        batch = self._by_id(progress, batch_id)
        previous = batch.get("request_id")
        batch.update(
            status="waiting_gpt",
            request_id=request_id,
            revision=revision,
            evidence_hashes=sorted(evidence_hashes),
            waiting_since=_utc_now(),
        )
        progress["next_revision"] = max(
            int(progress.get("next_revision", revision + 1)), revision + 1
        )
        if previous and previous != request_id:
            batch["retry_count"] = int(batch.get("retry_count", 0)) + 1
            batch.setdefault("request_lineage", []).append(previous)
        self.save(progress)

    def active_for_request(self, request_id: str | None = None) -> dict[str, Any] | None:
        progress = self.load()
        active_id = progress.get("active_batch_id")
        if not active_id:
            return None
        batch = self._by_id(progress, str(active_id))
        if request_id is not None and batch.get("request_id") != request_id:
            return None
        return batch

    def batch_for_request(self, request_id: str) -> dict[str, Any] | None:
        return next(
            (
                batch
                for batch in self.load().get("batches") or []
                if batch.get("request_id") == request_id
            ),
            None,
        )

    def replayed_refinement(
        self, request_id: str, response_sha256: str
    ) -> dict[str, Any] | None:
        progress = self.load()
        batch = next(
            (
                item
                for item in progress.get("batches") or []
                if item.get("request_id") == request_id
            ),
            None,
        )
        if (
            batch is None
            or batch.get("status") != "blocked_refinement"
            or not batch.get("blocked_by")
        ):
            return None
        if batch.get("response_sha256") != response_sha256:
            raise ValueError(
                "footage refinement request was already checkpointed differently"
            )
        children = [
            item
            for item in progress.get("batches") or []
            if item.get("batch_id") in set(batch["blocked_by"])
        ]
        return {
            "idempotent": True,
            "deferred_batch": self.public_batch(batch),
            "refinement_batches": [self.public_batch(item) for item in children],
            "group_ids": list(batch.get("refinement_group_ids") or []),
        }

    def selected_evidence(self) -> dict[str, Any] | None:
        """Return transport evidence for the active batch only."""
        batch = self.active_for_request()
        if batch is None:
            return None
        wanted = {
            (str(item["group_id"]), str(path))
            for item in batch.get("ranges") or []
            for path in item.get("frame_paths") or []
        }
        available = SampledFrameEvidenceStore(
            self.project, self.runner.state.revision
        ).available("footage")
        selected = []
        for group in available.get("groups") or []:
            frames = [
                frame
                for frame in group.get("frames") or []
                if (str(group.get("group_id")), str(frame.get("path"))) in wanted
            ]
            if frames:
                selected.append({**group, "frames": frames})
        expected = sum(len(item.get("frame_paths") or []) for item in batch["ranges"])
        actual = sum(len(item.get("frames") or []) for item in selected)
        if actual != expected:
            raise ValueError(
                f"footage batch {batch['batch_id']} evidence is incomplete: "
                f"{actual}/{expected} frames"
            )
        return {
            **available,
            "groups": selected,
            "frame_count": actual,
            "handoff_revision": batch["revision"],
            "selection": batch["batch_type"],
            "batch": self.public_batch(batch),
        }

    def validation_evidence(self) -> dict[str, Any] | None:
        """Return local validation evidence without expanding the transport packet.

        A resumed parent batch may use measured boundaries discovered by completed
        refinement children. Those child frames stay local here and are never
        re-exported in the parent's Drive request.
        """
        batch = self.active_for_request()
        selected = self.selected_evidence()
        if batch is None or selected is None:
            return selected

        progress = self.load()
        completed_children = {
            str(item["batch_id"]): item
            for item in progress.get("batches") or []
            if item.get("status") == "completed"
            and item.get("batch_id") in set(batch.get("blocked_by") or [])
        }
        if not completed_children:
            return selected

        wanted_group_ids = {
            str(item.get("group_id"))
            for child in completed_children.values()
            for item in child.get("ranges") or []
        }
        available = SampledFrameEvidenceStore(
            self.project, self.runner.state.revision
        ).available("footage")
        by_id = {
            str(group.get("group_id")): group
            for group in available.get("groups") or []
        }
        missing = sorted(wanted_group_ids - set(by_id))
        if missing:
            raise ValueError(
                "completed footage refinement evidence is no longer available: "
                + ", ".join(missing)
            )

        merged: dict[str, dict[str, Any]] = {
            str(group.get("group_id")): group
            for group in selected.get("groups") or []
        }
        for group_id in wanted_group_ids:
            merged[group_id] = by_id[group_id]
        groups = list(merged.values())
        return {
            **selected,
            "groups": groups,
            "frame_count": sum(len(group.get("frames") or []) for group in groups),
        }

    def write_active_scaffold(self) -> Path:
        batch = self.active_for_request()
        if batch is None:
            raise ValueError("no active footage semantic batch")
        scaffold = self.runner.artifacts.read("footage_profiles_scaffold")
        windows: dict[str, list[tuple[float, float]]] = {}
        for item in batch["ranges"]:
            windows.setdefault(str(item["clip_id"]), []).append(
                (float(item["start_seconds"]), float(item["end_seconds"]))
            )
        clips = []
        for source_clip in scaffold.get("clips") or []:
            clip_id = str(source_clip.get("clip_id") or "")
            if clip_id not in windows:
                continue
            clip = dict(source_clip)
            clip["segments"] = [
                segment
                for segment in source_clip.get("segments") or []
                if any(
                    float(segment.get("source_out", 0.0)) >= start - 1e-6
                    and float(segment.get("source_in", 0.0)) <= end + 1e-6
                    for start, end in windows[clip_id]
                )
            ]
            clips.append(clip)

        completed_prerequisites = [
            {
                "batch_id": child["batch_id"],
                "result": self.runner.artifacts.read(str(child["result_artifact"])),
            }
            for child in self._completed_refinement_children(batch)
            if child.get("result_artifact")
        ]
        compact = {
            "version": scaffold.get("version", "1.0"),
            "source_dir": scaffold.get("source_dir"),
            "clips": clips,
            "analysis_meta": {
                "semantic_enrichment_required": True,
                "batch_id": batch["batch_id"],
                "batch_type": batch["batch_type"],
                "ranges": self.public_batch(batch)["ranges"],
                "completed_refinement_results": completed_prerequisites,
            },
        }
        path = (
            self.project.cache_dir
            / "footage-batches"
            / f"{batch['batch_id']}-scaffold.json"
        )
        _atomic_json(path, compact)
        return path

    def complete(
        self, request_id: str, payload: dict[str, Any], response_sha256: str
    ) -> dict[str, Any]:
        progress = self.load()
        active_id = progress.get("active_batch_id")
        batch = self._by_id(progress, str(active_id)) if active_id else None
        if batch is not None and batch.get("request_id") != request_id:
            batch = None
        if batch is None:
            batch = next(
                (
                    item
                    for item in progress.get("batches") or []
                    if item.get("request_id") == request_id
                    and item.get("status") == "completed"
                ),
                None,
            )
        if batch is None:
            raise ValueError(
                "footage response does not match the active semantic batch"
            )
        if batch.get("status") == "completed":
            if batch.get("response_sha256") != response_sha256:
                raise ValueError(
                    "completed footage batch received a different response"
                )
            return {
                "idempotent": True,
                "batch": self.public_batch(batch),
                "final_payload": self.assemble_if_complete(),
                "remaining_batches": self.remaining_count(),
            }

        normalized = self._validate_partial(batch, payload)
        artifact_name = f"footage_semantic_batch_{batch['batch_id']}"
        artifact_path = self.runner.artifacts.write(artifact_name, normalized)
        previous = {
            key: batch.get(key)
            for key in (
                "status",
                "response_sha256",
                "result_artifact",
                "result_path",
                "completed_at",
            )
        }
        batch.update(
            status="completed",
            response_sha256=response_sha256,
            result_artifact=artifact_name,
            result_path=str(artifact_path),
            completed_at=_utc_now(),
        )
        progress["active_batch_id"] = None
        self.save(progress)
        try:
            final = self.assemble_if_complete()
        except Exception:
            # A final cross-batch invariant can fail even after a single partial
            # payload passed its local validator. Do not poison durable progress:
            # restore the waiting batch so GPT can submit a corrected response.
            for key, value in previous.items():
                if value is None:
                    batch.pop(key, None)
                else:
                    batch[key] = value
            progress["active_batch_id"] = batch["batch_id"]
            self.save(progress)
            try:
                artifact_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        return {
            "idempotent": False,
            "batch": self.public_batch(batch),
            "final_payload": final,
            "remaining_batches": self.remaining_count(),
        }

    def defer_for_refinement(
        self,
        request_id: str,
        group_ids: list[str],
        response_sha256: str,
    ) -> dict[str, Any]:
        progress = self.load()
        active_id = progress.get("active_batch_id")
        batch = self._by_id(progress, str(active_id)) if active_id else None
        if batch is not None and batch.get("request_id") != request_id:
            batch = None
        if batch is None:
            raise ValueError(
                "refinement response does not match the active semantic batch"
            )
        retry_count = int(batch.get("semantic_retry_count", 0)) + 1
        if retry_count > 3:
            batch.update(
                status="failed", error="semantic refinement retry limit reached"
            )
            self.save(progress)
            raise ValueError(
                "semantic refinement retry limit reached for active footage batch"
            )
        groups = [
            group
            for group in self._candidate_groups(kinds={"dense_refinement"})
            if str(group.get("group_id")) in set(group_ids)
        ]
        if len(groups) != len(set(group_ids)):
            raise ValueError("not all sampled refinement groups are available")
        self._assert_groups_within_batch(batch, groups)

        new_batches = self._pack(groups, start_index=len(progress["batches"]) + 1)
        batch.update(
            status="blocked_refinement",
            response_sha256=response_sha256,
            refinement_group_ids=sorted(group_ids),
            semantic_retry_count=retry_count,
            blocked_by=[item["batch_id"] for item in new_batches],
            last_refinement_at=_utc_now(),
        )
        progress["batches"].extend(new_batches)
        progress["active_batch_id"] = None
        self.save(progress)
        return {
            "deferred_batch": self.public_batch(batch),
            "refinement_batches": [self.public_batch(item) for item in new_batches],
        }

    def remaining_count(self) -> int:
        return sum(
            item.get("status") != "completed"
            for item in self.load().get("batches") or []
        )

    def assemble_if_complete(self) -> dict[str, Any] | None:
        progress = self.load()
        batches = progress.get("batches") or []
        if not batches or any(
            item.get("status") != "completed" for item in batches
        ):
            return None
        scaffold = self.runner.artifacts.read("footage_profiles_scaffold")
        results: dict[str, list[dict[str, Any]]] = {}
        for batch in batches:
            name = batch.get("result_artifact")
            if not name:
                continue
            payload = self.runner.artifacts.read(str(name))
            for clip in payload.get("clips") or []:
                results.setdefault(str(clip["clip_id"]), []).append(clip)

        assembled_clips = []
        for measured in scaffold.get("clips") or []:
            clip_id = str(measured["clip_id"])
            pieces = results.get(clip_id) or []
            segments: dict[str, dict[str, Any]] = {}
            summaries: list[str] = []
            risks: list[str] = []
            for piece in pieces:
                summary = str(piece.get("content_summary") or "").strip()
                if summary and summary not in summaries:
                    summaries.append(summary)
                for risk in piece.get("quality_risks") or []:
                    if str(risk) not in risks:
                        risks.append(str(risk))
                for segment in piece.get("segments") or []:
                    key = _canonical_hash(segment)
                    segments[key] = segment
            ordered = sorted(
                segments.values(),
                key=lambda item: (
                    float(item["source_in"]),
                    float(item["source_out"]),
                ),
            )
            clip = {
                "clip_id": clip_id,
                "path": measured.get("path"),
                "usable": bool(ordered),
                "content_summary": " ".join(summaries),
                "quality_risks": risks,
                "segments": ordered,
            }
            assembled_clips.append(clip)

        planner = FootageEvidencePlanner(
            self.project, self.runner.state.revision
        )
        payload = {
            "clips": assembled_clips,
            "evidence_catalog": planner.evidence_catalog(scaffold),
            "analysis_notes": [
                "Deterministically assembled from completed footage semantic batches."
            ],
        }
        sampled = SampledFrameEvidenceStore(
            self.project, self.runner.state.revision
        ).available("footage")
        _validate_footage(scaffold, payload, sampled.get("groups") or [])
        return payload

    def public_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        public = {
            key: batch.get(key)
            for key in (
                "batch_id",
                "sequence",
                "batch_type",
                "status",
                "revision",
                "clip_ids",
                "request_id",
                "retry_count",
                "frame_count",
                "evidence_bytes",
            )
        }
        public["ranges"] = [
            {
                key: item.get(key)
                for key in (
                    "group_id",
                    "clip_id",
                    "start_seconds",
                    "end_seconds",
                    "frame_count",
                    "evidence_bytes",
                )
            }
            for item in batch.get("ranges") or []
        ]
        return public

    def _validate_partial(
        self, batch: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        if payload.get("artifact") not in {
            None,
            "footage_semantic_batch",
            "footage_semantic_enrichment",
        }:
            raise ValueError("footage batch payload has an unsupported artifact")
        if payload.get("batch_id") != batch["batch_id"]:
            raise ValueError(
                "footage batch payload batch_id does not match active batch"
            )
        clips = payload.get("clips")
        if not isinstance(clips, list) or not clips:
            raise ValueError("footage semantic batch requires non-empty clips[]")
        actual = {
            str(item.get("clip_id") or "")
            for item in clips
            if isinstance(item, dict)
        }
        expected = set(batch["clip_ids"])
        if actual != expected or len(clips) != len(actual):
            raise ValueError(
                "footage batch clips must exactly match active batch: "
                f"expected={sorted(expected)}"
            )

        scaffold = self.runner.artifacts.read("footage_profiles_scaffold")
        subset = {
            **scaffold,
            "clips": [
                item
                for item in scaffold["clips"]
                if str(item["clip_id"]) in expected
            ],
        }
        selected = self.validation_evidence() or {"groups": []}
        normalized = {
            "artifact": "footage_semantic_batch",
            "batch_id": batch["batch_id"],
            "request_id": batch.get("request_id"),
            "revision": batch.get("revision"),
            "clips": clips,
        }
        _validate_footage(subset, normalized, selected.get("groups") or [])

        windows: dict[str, list[tuple[float, float]]] = {}
        for item in batch["ranges"]:
            windows.setdefault(str(item["clip_id"]), []).append(
                (float(item["start_seconds"]), float(item["end_seconds"]))
            )
        for clip in clips:
            for segment in clip.get("segments") or []:
                start = float(segment["source_in"])
                end = float(segment["source_out"])
                if not any(
                    start >= low - 1e-6 and end <= high + 1e-6
                    for low, high in windows[str(clip["clip_id"])]
                ):
                    raise ValueError(
                        "footage batch segment is outside active evidence ranges"
                    )

        self._assert_no_completed_refinement_overlap(batch, clips)
        return normalized

    def _candidate_groups(
        self, kinds: set[str] | None = None
    ) -> list[dict[str, Any]]:
        groups = SampledFrameEvidenceStore(
            self.project, self.runner.state.revision
        ).available("footage").get("groups") or []
        allowed = kinds if kinds is not None else {"coarse", "manual"}
        result = []
        for group in groups:
            kind = str(group.get("sample_kind") or "manual")
            if kind not in allowed:
                continue
            result.append(group)
        return sorted(
            result,
            key=lambda item: (
                0 if item.get("sample_kind") == "dense_refinement" else 1,
                str((item.get("source") or {}).get("clip_id") or ""),
                float(
                    (item.get("request") or {}).get("start_seconds") or 0.0
                ),
                str(item.get("group_id") or ""),
            ),
        )

    def _pack(
        self, groups: list[dict[str, Any]], *, start_index: int
    ) -> list[dict[str, Any]]:
        units: list[dict[str, Any]] = []
        for group in groups:
            frames = list(group.get("frames") or [])
            evidence_bytes = sum(
                Path(str(frame["absolute_path"])).stat().st_size
                for frame in frames
            )
            if (
                len(frames) > self.policy.max_frames
                or evidence_bytes > self.policy.max_evidence_bytes
            ):
                request = group.get("request") or {}
                raise ValueError(
                    "single footage evidence range exceeds batch budget: "
                    f"group={group.get('group_id')} "
                    f"window={request.get('start_seconds')}-"
                    f"{request.get('end_seconds')} "
                    f"frames={len(frames)}/{self.policy.max_frames} "
                    f"bytes={evidence_bytes}/{self.policy.max_evidence_bytes}"
                )
            if frames:
                units.append(self._unit(group, frames, evidence_bytes))

        batches: list[dict[str, Any]] = []
        current: list[dict[str, Any]] = []
        clips: set[str] = set()
        frames = total_bytes = 0
        for unit in units:
            next_clips = clips | {unit["clip_id"]}
            incompatible = (
                bool(current)
                and current[0]["batch_type"] != unit["batch_type"]
            )
            exceeds = bool(current) and (
                incompatible
                or len(next_clips) > self.policy.max_clips
                or len(current) >= self.policy.max_refinement_ranges
                or frames + unit["frame_count"] > self.policy.max_frames
                or total_bytes + unit["evidence_bytes"]
                > self.policy.max_evidence_bytes
            )
            if exceeds:
                batches.append(
                    self._batch(current, start_index + len(batches))
                )
                current, clips, frames, total_bytes = [], set(), 0, 0
            current.append(unit)
            clips.add(unit["clip_id"])
            frames += unit["frame_count"]
            total_bytes += unit["evidence_bytes"]
        if current:
            batches.append(self._batch(current, start_index + len(batches)))
        return batches

    @staticmethod
    def _unit(
        group: dict[str, Any], frames: list[dict[str, Any]], size: int
    ) -> dict[str, Any]:
        window = group.get("request") or {}
        return {
            "group_id": str(group["group_id"]),
            "clip_id": str((group.get("source") or {}).get("clip_id") or ""),
            "batch_type": (
                "refinement"
                if group.get("sample_kind") == "dense_refinement"
                else "coarse"
            ),
            "start_seconds": float(window.get("start_seconds") or 0.0),
            "end_seconds": float(window.get("end_seconds") or 0.0),
            "frame_paths": [str(item["path"]) for item in frames],
            "frame_count": len(frames),
            "evidence_bytes": size,
        }

    @staticmethod
    def _batch(
        units: list[dict[str, Any]], sequence: int
    ) -> dict[str, Any]:
        identity = [
            {
                key: item[key]
                for key in (
                    "group_id",
                    "clip_id",
                    "start_seconds",
                    "end_seconds",
                    "frame_paths",
                )
            }
            for item in units
        ]
        batch_type = units[0]["batch_type"]
        batch_id = (
            f"{batch_type}_{sequence:03d}_{_canonical_hash(identity)[:10]}"
        )
        return {
            "batch_id": batch_id,
            "sequence": sequence,
            "batch_type": batch_type,
            "status": "pending",
            "revision": None,
            "clip_ids": sorted({item["clip_id"] for item in units}),
            "ranges": units,
            "frame_count": sum(item["frame_count"] for item in units),
            "evidence_bytes": sum(item["evidence_bytes"] for item in units),
            "blocked_by": [],
            "retry_count": 0,
        }

    def _identity(
        self, scaffold: dict[str, Any], groups: list[dict[str, Any]]
    ) -> str:
        return _canonical_hash(
            {
                "pipeline_revision": self.runner.state.revision,
                "scaffold": scaffold,
                "policy": asdict(self.policy),
                "coarse_groups": [
                    {
                        "group_id": group.get("group_id"),
                        "source_sha256": (group.get("source") or {}).get(
                            "sha256"
                        ),
                    }
                    for group in groups
                    if group.get("sample_kind") != "dense_refinement"
                ],
            }
        )

    def _matches_current(
        self, current: dict[str, Any], identity: str
    ) -> bool:
        return (
            current.get("version") == PROGRESS_VERSION
            and current.get("pipeline_revision") == self.runner.state.revision
            and current.get("identity_sha256") == identity
            and current.get("policy") == asdict(self.policy)
            and isinstance(current.get("batches"), list)
        )

    def _completed_refinement_children(
        self, batch: dict[str, Any]
    ) -> list[dict[str, Any]]:
        blocked_by = set(batch.get("blocked_by") or [])
        if not blocked_by:
            return []
        return [
            item
            for item in self.load().get("batches") or []
            if item.get("batch_id") in blocked_by
            and item.get("batch_type") == "refinement"
            and item.get("status") == "completed"
        ]

    def _assert_no_completed_refinement_overlap(
        self,
        batch: dict[str, Any],
        clips: list[dict[str, Any]],
    ) -> None:
        children = self._completed_refinement_children(batch)
        if not children:
            return
        child_segments: dict[str, list[tuple[float, float]]] = {}
        for child in children:
            artifact = child.get("result_artifact")
            if not artifact:
                continue
            result = self.runner.artifacts.read(str(artifact))
            for clip in result.get("clips") or []:
                clip_id = str(clip.get("clip_id") or "")
                for segment in clip.get("segments") or []:
                    child_segments.setdefault(clip_id, []).append(
                        (
                            float(segment["source_in"]),
                            float(segment["source_out"]),
                        )
                    )
        for clip in clips:
            clip_id = str(clip.get("clip_id") or "")
            for segment in clip.get("segments") or []:
                start = float(segment["source_in"])
                end = float(segment["source_out"])
                for child_start, child_end in child_segments.get(
                    clip_id, []
                ):
                    if (
                        start < child_end - 1e-6
                        and end > child_start + 1e-6
                    ):
                        raise ValueError(
                            "footage parent batch overlaps completed "
                            "refinement semantics"
                        )

    def _assert_groups_within_batch(
        self,
        batch: dict[str, Any],
        groups: list[dict[str, Any]],
    ) -> None:
        windows: dict[str, list[tuple[float, float]]] = {}
        for item in batch.get("ranges") or []:
            windows.setdefault(str(item["clip_id"]), []).append(
                (float(item["start_seconds"]), float(item["end_seconds"]))
            )
        for group in groups:
            clip_id = str((group.get("source") or {}).get("clip_id") or "")
            request = group.get("request") or {}
            start = float(request.get("start_seconds") or 0.0)
            end = float(request.get("end_seconds") or 0.0)
            if not any(
                start >= low - 1e-6 and end <= high + 1e-6
                for low, high in windows.get(clip_id, [])
            ):
                raise ValueError(
                    "footage refinement evidence is outside the active batch"
                )

    @staticmethod
    def _by_id(
        progress: dict[str, Any], batch_id: str
    ) -> dict[str, Any]:
        for batch in progress.get("batches") or []:
            if batch.get("batch_id") == batch_id:
                return batch
        raise ValueError(f"unknown footage semantic batch: {batch_id}")
